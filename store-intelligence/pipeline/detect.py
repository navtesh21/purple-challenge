"""Detection pipeline entrypoint: CCTV clips -> structured behavioural events.

Flow per store:
  for each camera clip:
    YOLO person detection + ByteTrack tracking (ultralytics `model.track`)
    -> for each tracked person per frame:
         crop -> appearance signature -> ReIDGallery.assign() -> stable visitor_id
         centroid -> ZoneMapper -> current zone
         entry camera: LineCrossing -> ENTRY / EXIT / REENTRY
         floor/billing cameras: zone transitions -> ZONE_ENTER/EXIT/DWELL, queue
    emit schema-valid events (JSONL or HTTP)

Run against real footage:
    python pipeline/detect.py --clips data/clips --store STORE_BLR_002 \
        --layout data/store_layout.json --out events.jsonl

If footage / CV deps are absent, this exits with a clear message and points at the
simulator (`tools/simulate.py`), which streams the same schema for the live demo.

Heavy CV imports are deferred into `run()` so `--help` and import-for-test work
without ultralytics/opencv installed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import tracker as T  # noqa: E402
from pipeline.emit import HttpSink, JsonlSink, make_entry, make_queue, make_zone  # noqa: E402

# Frame timestamp = clip start + frame_index / fps. The clip start per camera is
# read from a sidecar (clips/<store>/<camera>.json: {"start":"...Z","fps":15}) or
# defaults below.
DEFAULT_FPS = 15
CROSS_COOLDOWN_S = 3.0   # min seconds between counted door-line crossings per visitor
ZONE_CONFIRM_FRAMES = 3  # consecutive frames a new zone must persist before switching (hysteresis)
MIN_SERVE_S = 15         # billing dwell below this with a queue present => abandoned, else served


def _load_layout(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _clip_start(clips_dir: Path, store: str, camera: str) -> tuple[datetime, float]:
    sidecar = clips_dir / store / f"{camera}.json"
    if sidecar.exists():
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        start = datetime.fromisoformat(meta["start"].replace("Z", "+00:00"))
        return start.astimezone(timezone.utc), float(meta.get("fps", DEFAULT_FPS))
    # deterministic default so timestamps are reproducible without a sidecar
    return datetime(2026, 3, 3, 9, 0, 0, tzinfo=timezone.utc), DEFAULT_FPS


def _centroid(xyxy) -> tuple[float, float]:
    x1, y1, x2, y2 = xyxy
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _cam_start_fps(clip_path, store, camera, cam_cfg) -> tuple[datetime, float]:
    """Clip start + fps: prefer explicit layout fields (real footage), then a sidecar
    JSON, then a deterministic default."""
    if cam_cfg.get("start"):
        start = datetime.fromisoformat(cam_cfg["start"].replace("Z", "+00:00"))
        return start.astimezone(timezone.utc), float(cam_cfg.get("fps", DEFAULT_FPS))
    return _clip_start(Path(clip_path).parent.parent, store, camera)


def process_camera(model, clip_path, store, camera, cam_cfg, gallery, staff_clf,
                   sink, layout, *, conf_threshold=0.25, vid_stride=1):
    """Run detection+tracking over one camera clip and emit events."""
    import cv2  # noqa: F401  (ultralytics pulls frames; cv2 used for crops)

    role = cam_cfg.get("role")
    # The back room is staff-only: nobody there is a customer, so force the staff flag
    # for everything this camera sees (real, useful staff signal — and exclusion).
    force_staff = role == "backroom"
    zone_mapper = T.ZoneMapper(cam_cfg)
    start_ts, fps = _cam_start_fps(clip_path, store, camera, cam_cfg)

    line = None
    if role == "entry":
        # door line from layout (cameras.<cam>.line_px: [[x1,y1],[x2,y2]]) or mid-frame
        lp = cam_cfg.get("line_px")
        line = T.LineCrossing(tuple(lp[0]), tuple(lp[1])) if lp else None

    prev_centroid: dict[str, tuple[float, float]] = {}
    last_cross: dict[str, datetime] = {}
    active: set[str] = set()                       # entered, not yet exited (entry cam)
    zstate: dict[str, T.DwellState] = {}           # floor: current zone per visitor
    zlast: dict[str, datetime] = {}                # floor: last ts a visitor was seen
    zhot: dict[str, tuple[float, float]] = {}      # floor: last centroid (zone hotspot)
    billing: dict[str, dict] = {}                  # billing: per-visitor episode

    results = model.track(source=str(clip_path), stream=True, persist=True,
                          tracker="bytetrack.yaml", classes=[0], conf=conf_threshold,
                          verbose=False, vid_stride=vid_stride)
    for step, res in enumerate(results):
        ts = start_ts + timedelta(seconds=(step * vid_stride) / fps)
        if res.boxes is None or res.boxes.id is None:
            continue
        frame = res.orig_img
        if frame is None:
            continue
        for box, tid, det_conf in zip(res.boxes.xyxy.tolist(),
                                      res.boxes.id.int().tolist(),
                                      res.boxes.conf.tolist()):
            x1, y1, x2, y2 = [int(v) for v in box]
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            sig = T.appearance_signature(crop)
            vid, is_reentry = gallery.assign(camera, tid, sig, ts)
            is_staff = force_staff or staff_clf.is_staff(crop)
            c = _centroid(box)
            conf = float(det_conf)

            if role == "entry":
                # re-entry owned by the gallery (re-match of an exited visitor)
                if is_reentry and vid not in active:
                    active.add(vid)
                    sink.emit(make_entry(store_id=store, camera_id=camera, visitor_id=vid,
                                         event_type="reentry", timestamp=ts, is_staff=is_staff, confidence=conf))
                if line is not None and vid in prev_centroid:
                    cross = line.crossing(prev_centroid[vid], c)
                    recent = vid in last_cross and (ts - last_cross[vid]).total_seconds() < CROSS_COOLDOWN_S
                    if cross and not recent:
                        if cross == "ENTRY" and not is_reentry and vid not in active:
                            active.add(vid); last_cross[vid] = ts
                            sink.emit(make_entry(store_id=store, camera_id=camera, visitor_id=vid,
                                                 event_type="entry", timestamp=ts, is_staff=is_staff, confidence=conf))
                        elif cross == "EXIT" and vid in active:
                            gallery.mark_exit(vid); active.discard(vid); last_cross[vid] = ts
                            sink.emit(make_entry(store_id=store, camera_id=camera, visitor_id=vid,
                                                 event_type="exit", timestamp=ts, is_staff=is_staff, confidence=conf))
                prev_centroid[vid] = c

            elif role in ("floor", "backroom"):
                zlast[vid], zhot[vid] = ts, c
                _floor_zone(sink, store, camera, vid, zone_mapper.zone_for(c), ts,
                            is_staff, conf, zstate, zhot, layout)

            elif role == "billing":
                # red-uniformed cashiers stand at the counter the whole time; they are NOT
                # customers in the queue, so don't track them (and they won't inflate depth).
                if is_staff:
                    continue
                b = billing.get(vid)
                if b is None:
                    # queue position = OTHER customers ACTUALLY PRESENT at join (seen within
                    # the last 5s), not a running total of everyone who ever visited billing.
                    present = sum(1 for o in billing.values()
                                  if (ts - o["last"]).total_seconds() <= 5)
                    billing[vid] = {"first": ts, "last": ts, "staff": False, "conf": conf, "pos": present}
                else:
                    b["last"], b["conf"] = ts, conf

    # close any floor zone still open at clip end so its ZONE_ENTER has a matching EXIT
    if role in ("floor", "backroom"):
        for vid, st in zstate.items():
            if st.zone is not None:
                zname, ztype = _zone_meta(layout, store, st.zone)
                sink.emit(make_zone(store_id=store, camera_id=camera, visitor_id=vid,
                                    event_type="zone_exited", timestamp=zlast.get(vid, st.entered_at),
                                    zone_id=st.zone, zone_name=zname, zone_type=ztype,
                                    hotspot=zhot.get(vid, (None, None)), confidence=0.9))
    # one queue event per billing visitor (completed unless a short wait while a queue existed)
    if role == "billing":
        bz = _billing_zone(layout, store) or "BILLING"
        for vid, b in billing.items():
            abandoned = (b["last"] - b["first"]).total_seconds() < MIN_SERVE_S and b["pos"] > 0
            sink.emit(make_queue(store_id=store, camera_id=camera, visitor_id=vid, abandoned=abandoned,
                                 zone_id=bz, join_ts=b["first"], served_ts=None if abandoned else b["first"],
                                 exit_ts=b["last"], queue_position=b["pos"], is_staff=b["staff"], confidence=b["conf"]))
    if hasattr(sink, "flush"):
        sink.flush()


def _zone_meta(layout, store, zone):
    """(zone_name, zone_type) for the wire event, from the layout's zone definition."""
    cfg = (layout.get("stores", {}).get(store, {}).get("zones", {}).get(zone) or {})
    ztype = {"product": "SHELF", "billing": "BILLING", "threshold": "THRESHOLD",
             "staff": "BOH"}.get(cfg.get("type"), "SHELF")
    return zone, ztype


def _floor_zone(sink, store, camera, vid, zone, ts, is_staff, conf, zstate, zhot, layout):
    """Emit zone_entered / zone_exited on confirmed zone changes (hysteresis kills border
    jitter). No periodic dwell event — dwell is the ENTER->EXIT gap, computed in analytics."""
    st = zstate.get(vid)
    if st is None:
        st = T.DwellState()
        zstate[vid] = st
    if zone != st.zone and st.zone is not None:
        if zone == st.pending_zone:
            st.pending_count += 1
        else:
            st.pending_zone, st.pending_count = zone, 1
        if st.pending_count < ZONE_CONFIRM_FRAMES:
            return
    st.pending_zone, st.pending_count = None, 0
    if zone == st.zone:
        return
    if st.zone is not None:
        zname, ztype = _zone_meta(layout, store, st.zone)
        sink.emit(make_zone(store_id=store, camera_id=camera, visitor_id=vid, event_type="zone_exited",
                            timestamp=ts, zone_id=st.zone, zone_name=zname, zone_type=ztype,
                            hotspot=zhot.get(vid, (None, None)), is_staff=is_staff, confidence=conf))
    if zone is not None:
        zname, ztype = _zone_meta(layout, store, zone)
        sink.emit(make_zone(store_id=store, camera_id=camera, visitor_id=vid, event_type="zone_entered",
                            timestamp=ts, zone_id=zone, zone_name=zname, zone_type=ztype,
                            hotspot=zhot.get(vid, (None, None)), is_staff=is_staff, confidence=conf))
    st.zone, st.entered_at = zone, ts


def _billing_zone(layout, store):
    zones = layout.get("stores", {}).get(store, {}).get("zones", {})
    for name, cfg in zones.items():
        if (cfg or {}).get("type") == "billing":
            return name
    return None


def _sku(layout, store, zone):
    if zone is None:
        return None
    return (layout.get("stores", {}).get(store, {}).get("zones", {}).get(zone, {}) or {}).get("sku_zone")


def _resolve_clip(args, layout_path, store, camera, cam_cfg) -> Path | None:
    """Locate a camera's video. Priority: explicit `video` in the layout (real
    footage, resolved relative to the layout file), then <clips>/<store>/<camera>.mp4."""
    if cam_cfg.get("video"):
        p = (Path(layout_path).resolve().parent / cam_cfg["video"]).resolve()
        return p if p.exists() else None
    if args.clips:
        p = Path(args.clips) / store / f"{camera}.mp4"
        return p if p.exists() else None
    return None


def run(argv=None):
    ap = argparse.ArgumentParser(description="CCTV -> behavioural events")
    ap.add_argument("--clips", help="clips dir: <clips>/<store>/<camera>.mp4 (omit if layout has `video` paths)")
    ap.add_argument("--store", required=True)
    ap.add_argument("--layout", required=True)
    ap.add_argument("--model", default="yolov8n.pt", help="YOLO weights")
    ap.add_argument("--out", help="write events to this JSONL file")
    ap.add_argument("--api", help="POST events to this API base URL instead of / in addition to file")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--vid-stride", type=int, default=1, help="process every Nth frame (CPU speed)")
    ap.add_argument("--reid-threshold", type=float, default=0.72,
                    help="Re-ID cosine similarity to call a re-entry/cross-camera match; "
                         "higher = stricter (fewer false re-entries, but track fragmentation "
                         "may split a person into more visitors)")
    args = ap.parse_args(argv)

    try:
        from ultralytics import YOLO
    except ImportError:
        print("ultralytics not installed. `pip install -r pipeline/requirements.txt`, "
              "or use tools/simulate.py for the live demo without footage.", file=sys.stderr)
        return 2

    layout = _load_layout(Path(args.layout))
    store_cfg = layout["stores"][args.store]
    model = YOLO(args.model)
    gallery = T.ReIDGallery(sim_threshold=args.reid_threshold)
    # Read the uniform colour(s) for this store from the layout (e.g. ["red"] or ["dark"]).
    # Falls back to ["red"] if the field is absent so older layout files still work.
    uniform_colors = store_cfg.get("staff_uniform", ["red"])
    staff_clf = T.StaffClassifier(uniform_colors=uniform_colors)

    sink = JsonlSink(args.out) if args.out else HttpSink(args.api)
    if args.out and args.api:
        sink = JsonlSink(args.out)  # file is primary; API replay handled by simulator

    for camera, cam_cfg in store_cfg["cameras"].items():
        clip = _resolve_clip(args, args.layout, args.store, camera, cam_cfg)
        if clip is None:
            print(f"skip {camera}: video not found", file=sys.stderr)
            continue
        print(f"processing {args.store}/{camera} ({cam_cfg.get('role')}) <- {clip.name} ...", file=sys.stderr)
        process_camera(model, clip, args.store, camera, cam_cfg, gallery, staff_clf, sink, layout,
                       conf_threshold=args.conf, vid_stride=args.vid_stride)
    sink.close()
    n = getattr(sink, "count", getattr(sink, "sent", 0))
    print(f"emitted {n} events", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
