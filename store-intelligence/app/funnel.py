"""GET /stores/{id}/funnel — Entry -> Zone Visit -> Billing Queue -> Purchase.

The unit is the session (visitor_id), so a visitor who re-enters is counted once
per stage, never twice. Drop-off is reported as the % lost from the previous stage.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.analytics import build_window, default_window


def compute_funnel(
    session: Session, store_id: str, start: datetime | None = None, end: datetime | None = None
) -> dict:
    if start is None or end is None:
        start, end = default_window(session, store_id)
    win = build_window(session, store_id, start, end)

    s = list(win.sessions.values())

    # Count each session at the DEEPEST funnel stage it reached, then make the stages
    # cumulative (stage i = sessions whose deepest stage >= i). This is both logically
    # correct (you cannot buy without having entered/browsed/queued, even if the camera
    # that would have shown the earlier step missed you) and monotonic by construction —
    # important for multi-camera footage where a billing-camera visitor may never have
    # been stitched to a floor-camera track (e.g. Store 2's un-synced cameras).
    def _depth(v) -> int:
        if v.converted:        # bought  -> implies billing, zone, entry
            return 4
        if v.reached_billing:  # queued  -> implies zone, entry
            return 3
        if v.zones_visited:    # browsed -> implies entry
            return 2
        return 1               # entered
    depths = [_depth(v) for v in s]
    entry = sum(1 for d in depths if d >= 1)   # == len(s); every session entered
    zone_visit = sum(1 for d in depths if d >= 2)
    billing_queue = sum(1 for d in depths if d >= 3)
    purchase = sum(1 for d in depths if d >= 4)

    counts = [
        ("ENTRY", entry),
        ("ZONE_VISIT", zone_visit),
        ("BILLING_QUEUE", billing_queue),
        ("PURCHASE", purchase),
    ]

    stages = []
    prev = None
    for name, count in counts:
        if prev is None or prev == 0:
            drop = 0.0
        else:
            drop = round((prev - count) / prev, 4)
        stages.append(
            {
                "stage": name,
                "count": count,
                "drop_off_from_previous": max(drop, 0.0),
                "pct_of_entry": round(count / entry, 4) if entry else 0.0,
            }
        )
        prev = count

    return {
        "store_id": store_id,
        "window": {"start": win.start.isoformat(), "end": win.end.isoformat()},
        "sessions": len(s),
        "stages": stages,
        "overall_conversion_rate": win.conversion_rate,
    }
