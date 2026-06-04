"""Event replay / live simulator — the bridge that makes the pipeline and API
"genuinely connected, not just batch-processed" (the live-dashboard bonus).

Two modes:

  --from-file events.jsonl     Backfill: POST a recorded event log into the API in
                               batches. Fast by default; --rt paces it by real time.

  --live                       Stream freshly-generated visitors in real time, stamped
                               at wall-clock now and POSTed immediately, so /health stays
                               fresh, /metrics ticks up live, and the dashboard animates.
                               The stream is LAYOUT-DRIVEN: it reads the store's real zones
                               and camera ids from store_layout.json, so it stays coherent
                               with whatever store you point it at (e.g. the real ST1008).
                               It periodically builds a billing queue so the anomaly
                               endpoint shows live signal too.

Both modes go through pipeline.emit (the one schema-valid event path).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import layout as L  # noqa: E402
from pipeline.emit import HttpSink, make_entry, make_zone, make_queue  # noqa: E402


def replay_file(api: str, path: Path, batch: int, rt_scale: float | None) -> None:
    sink = HttpSink(api, batch_size=batch)
    # utf-8-sig tolerates a BOM, which Windows tooling sometimes prepends to logs.
    events = [json.loads(l) for l in path.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
    events.sort(key=lambda e: e["timestamp"])
    print(f"replaying {len(events)} events from {path.name} -> {api}")

    prev_ts, sent = None, 0
    for ev in events:
        if rt_scale:
            ts = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
            if prev_ts is not None:
                gap = (ts - prev_ts).total_seconds() * rt_scale
                if gap > 0:
                    time.sleep(min(gap, 2.0))
            prev_ts = ts
        sink.emit(ev)
        sent += 1
        if sent % 2000 == 0:
            print(f"  ... {sent}")
    sink.close()
    print(f"done: {sink.sent} events posted")


def _camera_for(store: str, role: str, default: str) -> str:
    cams = L.store_config(store).get("cameras", {})
    for cam, cfg in cams.items():
        if (cfg or {}).get("role") == role:
            return cam
    return default


def live_stream(api: str, store: str, rng: random.Random, duration_s: int, rate_s: float) -> None:
    """Continuously spawn visitors using the store's real zones/cameras."""
    sink = HttpSink(api, batch_size=1)  # flush each visitor immediately for liveness
    zones = L.product_zones(store) or ["SKIN", "MAKEUP"]
    billing = L.billing_zone(store) or "BILLING"
    cam_entry = _camera_for(store, "entry", "CAM_ENTRY_01")
    cam_floor = _camera_for(store, "floor", "CAM_FLOOR_01")
    cam_bill = _camera_for(store, "billing", "CAM_BILLING_01")
    cold = zones[-1] if len(zones) > 1 else None  # keep one zone cold -> dead-zone anomaly

    print(f"live stream -> {api}  store={store}  zones={zones}  (Ctrl-C to stop)")
    ENTRY_TYPES = {"ENTRY", "EXIT", "REENTRY"}
    ZONE_TYPES  = {"ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"}

    start, spawned, vid_n, rush_run = time.monotonic(), 0, 0, 0
    try:
        while time.monotonic() - start < duration_s:
            now = datetime.now(timezone.utc)
            vid_n += 1
            vid = f"VIS_LIVE_{vid_n:05d}"
            is_staff = rng.random() < 0.1
            conf = round(rng.uniform(0.55, 0.97), 2)

            def emit(etype, *, camera, zone=None, dwell_ms=0, queue_depth=None, when=None):
                ts = when or datetime.now(timezone.utc)
                if etype in ENTRY_TYPES:
                    ev = make_entry(
                        store_id=store, camera_id=camera, visitor_id=vid,
                        event_type=etype.lower(), timestamp=ts,
                        is_staff=is_staff, confidence=conf,
                    )
                elif etype in ZONE_TYPES:
                    # Map internal type → wire type
                    wire_type = {"ZONE_ENTER": "zone_entered", "ZONE_EXIT": "zone_exited",
                                 "ZONE_DWELL": "zone_exited"}.get(etype, "zone_exited")
                    ev = make_zone(
                        store_id=store, camera_id=camera, visitor_id=vid,
                        event_type=wire_type, timestamp=ts, zone_id=zone,
                        is_staff=is_staff, confidence=conf,
                    )
                else:  # BILLING_QUEUE_JOIN / BILLING_QUEUE_ABANDON
                    dwell_s = max(1, dwell_ms // 1000) if dwell_ms else rng.randint(30, 180)
                    exit_ts = ts + timedelta(seconds=dwell_s)
                    abandoned = (etype == "BILLING_QUEUE_ABANDON")
                    ev = make_queue(
                        store_id=store, camera_id=camera, visitor_id=vid,
                        abandoned=abandoned, zone_id=zone or billing,
                        join_ts=ts, served_ts=(None if abandoned else ts),
                        exit_ts=exit_ts,
                        queue_position=queue_depth or 0,
                        is_staff=is_staff, confidence=conf,
                    )
                sink.emit(ev)

            emit("ENTRY", camera=cam_entry)
            for z in rng.sample(zones, k=min(len(zones), rng.randint(1, 2))):
                if z == cold:
                    continue
                emit("ZONE_ENTER", camera=cam_floor, zone=z)
                emit("ZONE_EXIT", camera=cam_floor, zone=z, dwell_ms=rng.randint(30, 180) * 1000)

            # periodic billing rush → climbing queue depth → queue-spike anomaly
            rush = (spawned % 12) in (5, 6, 7, 8) and not is_staff
            if rush or (not is_staff and rng.random() < 0.4):
                rush_run = rush_run + 1 if rush else 0
                qd = (4 + rush_run) if rush else rng.randint(0, 2)
                if qd >= 8 and rng.random() < 0.5:
                    emit("BILLING_QUEUE_ABANDON", camera=cam_bill, zone=billing, queue_depth=qd)
                else:
                    emit("BILLING_QUEUE_JOIN", camera=cam_bill, zone=billing, queue_depth=qd)
            emit("EXIT", camera=cam_entry)

            spawned += 1
            tag = "  [RUSH]" if rush else ("  [staff]" if is_staff else "")
            result = sink.flush()
            accepted = result.get("accepted", "?") if result else "?"
            print(f"  +visitor {vid}{tag}  → accepted {accepted} events  (total {sink.sent})")
            time.sleep(rate_s)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        sink.close()
    print(f"spawned {spawned} visitors")


def main(argv=None):
    ap = argparse.ArgumentParser(description="replay / live-simulate events into the API")
    ap.add_argument("--api", default="http://localhost:8000", help="API base URL")
    ap.add_argument("--from-file", help="replay this JSONL event log")
    ap.add_argument("--live", action="store_true", help="stream fresh events in real time")
    ap.add_argument("--store", default="ST1008")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--rt", type=float, default=None, help="real-time scale for --from-file (0.01=100x)")
    ap.add_argument("--duration", type=int, default=600, help="--live run seconds")
    ap.add_argument("--rate", type=float, default=2.0, help="--live seconds between visitors")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    if args.from_file:
        replay_file(args.api, Path(args.from_file), args.batch, args.rt)
    elif args.live:
        live_stream(args.api, args.store, rng, args.duration, args.rate)
    else:
        ap.error("choose --from-file <path> or --live")


if __name__ == "__main__":
    main()
