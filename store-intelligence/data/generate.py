"""Synthetic dataset + event generator.

Two jobs:
1. Write a self-consistent dataset (store_layout.json, pos_transactions.csv,
   sample_events.jsonl) so the whole system runs and is testable WITHOUT the
   challenge video ZIP. The real detection pipeline emits the *identical* schema,
   so swapping real footage in changes nothing downstream.
2. Be importable by the live simulator, which reuses `visitor_session()` to stream
   realistic, edge-case-rich events in simulated real-time.

Determinism: a seeded `random.Random` makes every run reproducible (important for
tests and for being able to reason about expected counts).

Modelled edge cases (the 7 from the brief):
  group entry, staff movement, re-entry, partial occlusion, billing-queue buildup,
  empty periods, and camera-angle overlap (we never emit the same person twice, the
  way a correct cross-camera dedup would behave).
"""
from __future__ import annotations

import csv
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The real challenge data lives directly in data/ (store_layout.json, pos_transactions.csv,
# events.jsonl). The synthetic generator writes to data/synthetic/ so it can never clobber
# the real, footage-derived files; it exists only as a dev/test fallback and to drive the
# live simulator's in-memory event stream.
DATA_DIR = Path(__file__).resolve().parent / "synthetic"

# --- store topology ---------------------------------------------------------

PRODUCT_ZONES = ["SKINCARE", "MAKEUP", "HAIRCARE", "FRAGRANCE", "WELLNESS"]
SKU_BY_ZONE = {
    "SKINCARE": "MOISTURISER",
    "MAKEUP": "LIPSTICK",
    "HAIRCARE": "SHAMPOO",
    "FRAGRANCE": "PERFUME",
    "WELLNESS": "SUPPLEMENTS",
}
BILLING_ZONE = "BILLING"
ENTRANCE_ZONE = "ENTRANCE"

CAM_ENTRY = "CAM_ENTRY_01"
CAM_FLOOR = "CAM_FLOOR_01"
CAM_BILLING = "CAM_BILLING_01"

STORES = {
    "STORE_BLR_002": "Bengaluru",
    "STORE_BLR_001": "Bengaluru",
    "STORE_DEL_001": "Delhi",
    "STORE_MUM_001": "Mumbai",
    "STORE_HYD_001": "Hyderabad",
}


def build_layout() -> dict:
    zones = {ENTRANCE_ZONE: {"type": "threshold"}}
    for z in PRODUCT_ZONES:
        zones[z] = {"type": "product", "sku_zone": SKU_BY_ZONE[z]}
    zones[BILLING_ZONE] = {"type": "billing"}

    stores = {}
    for sid, city in STORES.items():
        stores[sid] = {
            "city": city,
            "open_hours": {"open": "09:00", "close": "21:00", "tz": "Asia/Kolkata"},
            "cameras": {
                CAM_ENTRY: {"role": "entry", "covers": [ENTRANCE_ZONE]},
                CAM_FLOOR: {"role": "floor", "covers": PRODUCT_ZONES},
                CAM_BILLING: {"role": "billing", "covers": [BILLING_ZONE]},
            },
            "zones": zones,
        }
    return {"stores": stores}


# --- visitor planning -------------------------------------------------------


@dataclass
class VisitorPlan:
    vid: str
    entry_time: datetime
    is_staff: bool
    zones: list[str]
    go_billing: bool
    occluded: bool
    reenter: bool
    # filled in for billing visitors
    billing_join: datetime | None = None
    service_s: int = 0
    queue_depth: int = 0
    abandon: bool = False
    convert: bool = False
    txn_value: float = 0.0


def _new_vid(rng: random.Random) -> str:
    return "VIS_" + "".join(rng.choice("0123456789abcdef") for _ in range(6))


def plan_day(
    store_id: str,
    day: datetime,
    rng: random.Random,
    *,
    n_visitors: int,
    p_billing: float,
    p_convert: float,
    cold_zone: str | None,
    rush: bool,
    open_hour: int = 9,
    close_hour: int = 21,
) -> list[VisitorPlan]:
    """Plan a day's visitors, then resolve billing queue depth with a real
    arrival/service model so queue buildup and abandonment emerge naturally."""
    plans: list[VisitorPlan] = []
    span_s = (close_hour - open_hour) * 3600

    i = 0
    while i < n_visitors:
        # entry time across the open day, with an optional midday rush cluster
        if rush and rng.random() < 0.35:
            # rush window ~ 5 hours in (a tight cluster -> queue spike)
            base = open_hour * 3600 + 5 * 3600 + rng.randint(0, 1200)
        else:
            base = open_hour * 3600 + rng.randint(0, span_s)
        entry_time = day.replace(hour=open_hour, minute=0, second=0, microsecond=0) + timedelta(
            seconds=base - open_hour * 3600
        )

        is_staff = rng.random() < 0.12
        # group entry: 2-4 people within a couple of seconds
        group = 1
        if not is_staff and rng.random() < 0.18:
            group = rng.randint(2, 4)

        for g in range(group):
            if i >= n_visitors:
                break
            avail = [z for z in PRODUCT_ZONES if z != cold_zone]
            k = rng.randint(2, 4) if is_staff else rng.randint(1, 3)
            zones = rng.sample(avail, min(k, len(avail)))
            go_billing = (not is_staff) and (rng.random() < p_billing)
            plans.append(
                VisitorPlan(
                    vid=_new_vid(rng),
                    entry_time=entry_time + timedelta(seconds=g),
                    is_staff=is_staff,
                    zones=zones if not is_staff else PRODUCT_ZONES.copy(),
                    go_billing=go_billing,
                    occluded=rng.random() < 0.15,
                    reenter=(not is_staff) and rng.random() < 0.08,
                )
            )
            i += 1

    # resolve billing: join time, service time, queue depth, convert/abandon
    billing = [p for p in plans if p.go_billing]
    for p in billing:
        # arrive at billing after browsing
        browse_s = rng.randint(120, 1200)
        p.billing_join = p.entry_time + timedelta(seconds=browse_s)
        p.service_s = rng.randint(60, 180)

    billing.sort(key=lambda p: p.billing_join)
    for p in billing:
        t = p.billing_join
        # people ahead = those already being served whose service hasn't ended
        ahead = sum(
            1
            for o in billing
            if o is not p
            and o.billing_join <= t
            and o.billing_join + timedelta(seconds=o.service_s) > t
        )
        p.queue_depth = ahead
        # long queues drive abandonment
        p.abandon = ahead >= 6 and rng.random() < 0.5
        if not p.abandon:
            p.convert = rng.random() < p_convert
            if p.convert:
                p.txn_value = round(rng.uniform(200, 3200), 2)
    return plans


# --- event emission ---------------------------------------------------------


def _ev(
    store_id, camera, vid, etype, ts, seq, *, zone=None, dwell_ms=0, is_staff=False,
    confidence=0.92, queue_depth=None, sku_zone=None,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera,
        "visitor_id": vid,
        "event_type": etype,
        "timestamp": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone,
        "dwell_ms": int(dwell_ms),
        "is_staff": is_staff,
        "confidence": round(confidence, 2),
        "metadata": {"queue_depth": queue_depth, "sku_zone": sku_zone, "session_seq": seq},
    }


def visitor_session(store_id: str, plan: VisitorPlan, rng: random.Random) -> tuple[list[dict], list[dict]]:
    """Emit the ordered events for one visitor plan, plus any POS txn it produces."""
    events: list[dict] = []
    pos: list[dict] = []
    seq = 0
    conf_base = 0.55 if plan.occluded else rng.uniform(0.85, 0.97)

    def conf():
        return max(0.3, min(0.99, conf_base + rng.uniform(-0.05, 0.05)))

    t = plan.entry_time
    seq += 1
    events.append(_ev(store_id, CAM_ENTRY, plan.vid, "ENTRY", t, seq,
                      is_staff=plan.is_staff, confidence=conf()))

    # browse product zones
    for z in plan.zones:
        t += timedelta(seconds=rng.randint(20, 90))
        seq += 1
        events.append(_ev(store_id, CAM_FLOOR, plan.vid, "ZONE_ENTER", t, seq,
                          zone=z, is_staff=plan.is_staff, confidence=conf(),
                          sku_zone=SKU_BY_ZONE.get(z)))
        dwell_total = rng.randint(15, 240) * 1000
        # ZONE_DWELL every 30s of continued dwell (cumulative dwell_ms)
        elapsed = 30000
        while elapsed < dwell_total:
            seq += 1
            events.append(_ev(store_id, CAM_FLOOR, plan.vid, "ZONE_DWELL",
                              t + timedelta(milliseconds=elapsed), seq, zone=z,
                              dwell_ms=elapsed, is_staff=plan.is_staff, confidence=conf(),
                              sku_zone=SKU_BY_ZONE.get(z)))
            elapsed += 30000
        t += timedelta(milliseconds=dwell_total)
        seq += 1
        events.append(_ev(store_id, CAM_FLOOR, plan.vid, "ZONE_EXIT", t, seq, zone=z,
                          dwell_ms=dwell_total, is_staff=plan.is_staff, confidence=conf(),
                          sku_zone=SKU_BY_ZONE.get(z)))

    # billing
    if plan.go_billing and plan.billing_join is not None:
        t = plan.billing_join
        seq += 1
        if plan.queue_depth > 0:
            events.append(_ev(store_id, CAM_BILLING, plan.vid, "BILLING_QUEUE_JOIN", t, seq,
                              zone=BILLING_ZONE, queue_depth=plan.queue_depth,
                              is_staff=plan.is_staff, confidence=conf()))
        else:
            events.append(_ev(store_id, CAM_BILLING, plan.vid, "ZONE_ENTER", t, seq,
                              zone=BILLING_ZONE, is_staff=plan.is_staff, confidence=conf()))
        if plan.abandon:
            t += timedelta(seconds=rng.randint(30, 120))
            seq += 1
            events.append(_ev(store_id, CAM_BILLING, plan.vid, "BILLING_QUEUE_ABANDON", t, seq,
                              zone=BILLING_ZONE, queue_depth=plan.queue_depth,
                              is_staff=plan.is_staff, confidence=conf()))
        else:
            served_at = t + timedelta(seconds=plan.service_s)
            seq += 1
            events.append(_ev(store_id, CAM_BILLING, plan.vid, "ZONE_EXIT", served_at, seq,
                              zone=BILLING_ZONE, dwell_ms=plan.service_s * 1000,
                              is_staff=plan.is_staff, confidence=conf()))
            if plan.convert:
                pos.append({
                    "store_id": store_id,
                    "transaction_id": "TXN_" + uuid.uuid4().hex[:10].upper(),
                    "timestamp": served_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "basket_value_inr": f"{plan.txn_value:.2f}",
                })
            t = served_at

    # exit
    t += timedelta(seconds=rng.randint(10, 60))
    seq += 1
    events.append(_ev(store_id, CAM_ENTRY, plan.vid, "EXIT", t, seq,
                      is_staff=plan.is_staff, confidence=conf()))

    # re-entry: same visitor_id reappears (Re-ID caught it -> not a new visitor)
    if plan.reenter:
        t += timedelta(seconds=rng.randint(120, 600))
        seq += 1
        events.append(_ev(store_id, CAM_ENTRY, plan.vid, "REENTRY", t, seq,
                          is_staff=plan.is_staff, confidence=conf()))
        z = rng.choice(plan.zones)
        t += timedelta(seconds=rng.randint(20, 60))
        seq += 1
        events.append(_ev(store_id, CAM_FLOOR, plan.vid, "ZONE_ENTER", t, seq, zone=z,
                          is_staff=plan.is_staff, confidence=conf(), sku_zone=SKU_BY_ZONE.get(z)))
        t += timedelta(seconds=rng.randint(30, 120))
        seq += 1
        events.append(_ev(store_id, CAM_ENTRY, plan.vid, "EXIT", t, seq,
                          is_staff=plan.is_staff, confidence=conf()))

    return events, pos


def generate_day(store_id, day, rng, **kw) -> tuple[list[dict], list[dict]]:
    plans = plan_day(store_id, day, rng, **kw)
    events, pos = [], []
    for p in plans:
        e, px = visitor_session(store_id, p, rng)
        events.extend(e)
        pos.extend(px)
    events.sort(key=lambda e: e["timestamp"])
    return events, pos


def generate_dataset(out_dir: Path = DATA_DIR, *, days: int = 8, seed: int = 7) -> dict:
    """Write store_layout.json, pos_transactions.csv, sample_events.jsonl.

    Earlier days carry a healthy conversion baseline; the most recent day is
    deliberately depressed (lower conversion, a cold zone, a billing rush) so the
    anomaly detectors have something real to find.
    """
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    layout = build_layout()
    (out_dir / "store_layout.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")

    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    all_events: list[dict] = []
    all_pos: list[dict] = []
    for sid in STORES:
        for d in range(days):
            day = base - timedelta(days=d)
            today = d == 0
            ev, px = generate_day(
                sid, day, rng,
                n_visitors=rng.randint(60, 110),
                p_billing=0.55 if not today else 0.45,
                p_convert=0.7 if not today else 0.4,        # today's conversion sags
                cold_zone="WELLNESS" if today else None,    # today's dead zone
                rush=today,                                  # today's queue spike
            )
            all_events.extend(ev)
            all_pos.extend(px)

    all_events.sort(key=lambda e: e["timestamp"])
    all_pos.sort(key=lambda p: p["timestamp"])

    with open(out_dir / "pos_transactions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["store_id", "transaction_id", "timestamp", "basket_value_inr"])
        for p in all_pos:
            w.writerow([p["store_id"], p["transaction_id"], p["timestamp"], p["basket_value_inr"]])

    with open(out_dir / "sample_events.jsonl", "w", encoding="utf-8") as fh:
        for e in all_events[:200]:
            fh.write(json.dumps(e) + "\n")

    # full event log for fast replay into the API (the historical backfill)
    with open(out_dir / "events.jsonl", "w", encoding="utf-8") as fh:
        for e in all_events:
            fh.write(json.dumps(e) + "\n")

    return {"events": len(all_events), "pos": len(all_pos), "stores": len(STORES), "days": days}


if __name__ == "__main__":
    stats = generate_dataset()
    print(json.dumps(stats, indent=2))
