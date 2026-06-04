"""Event builders for tests. Kept out of the test_* namespace so pytest doesn't
collect it. Timestamps default to a fixed date with no seeded POS, so the
default_window anchors cleanly onto the test's own events."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

# A fixed operating day used across tests (no real POS rows fall on it).
DAY = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
STORE = "STORE_BLR_002"
BILLING = "BILLING"


def mk(event_type, *, vid, store=STORE, camera="CAM_FLOOR_01", t=None, zone=None,
       dwell_ms=0, is_staff=False, confidence=0.9, queue_depth=None, sku_zone=None, seq=1):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store,
        "camera_id": camera,
        "visitor_id": vid,
        "event_type": event_type,
        "timestamp": (t or DAY).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": sku_zone, "session_seq": seq},
    }


def visitor(vid, *, store=STORE, t0=None, zones=("SKINCARE",), billing=False,
            convert_at=None, abandon=False, queue_depth=0, is_staff=False, reentry=False):
    """Build a full ordered event list for one visitor.

    `convert_at` is ignored here (POS rows are added separately via add_pos); the
    billing presence it implies is created when billing=True.
    """
    t = t0 or DAY
    evs = [mk("ENTRY", vid=vid, store=store, camera="CAM_ENTRY_01", t=t, is_staff=is_staff, seq=1)]
    seq = 1
    for z in zones:
        t += timedelta(seconds=30)
        seq += 1
        evs.append(mk("ZONE_ENTER", vid=vid, store=store, t=t, zone=z, is_staff=is_staff, seq=seq))
        t += timedelta(seconds=60)
        seq += 1
        evs.append(mk("ZONE_EXIT", vid=vid, store=store, t=t, zone=z, dwell_ms=60000, is_staff=is_staff, seq=seq))
    if billing:
        t += timedelta(seconds=30)
        seq += 1
        evs.append(mk("BILLING_QUEUE_JOIN", vid=vid, store=store, camera="CAM_BILLING_01", t=t,
                      zone=BILLING, queue_depth=queue_depth, is_staff=is_staff, seq=seq))
        if abandon:
            t += timedelta(seconds=60)
            seq += 1
            evs.append(mk("BILLING_QUEUE_ABANDON", vid=vid, store=store, camera="CAM_BILLING_01",
                          t=t, zone=BILLING, queue_depth=queue_depth, is_staff=is_staff, seq=seq))
    t += timedelta(seconds=30)
    seq += 1
    evs.append(mk("EXIT", vid=vid, store=store, camera="CAM_ENTRY_01", t=t, is_staff=is_staff, seq=seq))
    if reentry:
        t += timedelta(seconds=300)
        seq += 1
        evs.append(mk("REENTRY", vid=vid, store=store, camera="CAM_ENTRY_01", t=t, is_staff=is_staff, seq=seq))
        t += timedelta(seconds=30)
        seq += 1
        evs.append(mk("ZONE_ENTER", vid=vid, store=store, t=t, zone=zones[0], is_staff=is_staff, seq=seq))
        t += timedelta(seconds=30)
        seq += 1
        evs.append(mk("EXIT", vid=vid, store=store, camera="CAM_ENTRY_01", t=t, is_staff=is_staff, seq=seq))
    return evs


def billing_time(t0=None, n_zones=1):
    """Billing-presence time produced by `visitor(... billing=True)`:
    ENTRY(t0) + per-zone(30 enter + 60 exit) + 30 to reach billing."""
    return (t0 or DAY) + timedelta(seconds=n_zones * 90 + 30)
