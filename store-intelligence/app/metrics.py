"""GET /stores/{id}/metrics — the day's headline numbers.

Real-time (computed from the freshest events, never cached), staff-excluded,
and zero-traffic safe: an empty or zero-purchase store returns well-formed zeros,
never null and never a 500.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics import build_window, default_window
from app.db import EventRow, PosRow, to_naive_utc


def compute_metrics(
    session: Session, store_id: str, start: datetime | None = None, end: datetime | None = None
) -> dict:
    if start is None or end is None:
        start, end = default_window(session, store_id)
    win = build_window(session, store_id, start, end)

    # avg dwell per zone (seconds), averaged over visitors who dwelt there
    dwell_acc: dict[str, list[int]] = {}
    abandon_billing = 0
    reached_billing = 0
    for s in win.sessions.values():
        for zone, ms in s.dwell_by_zone.items():
            dwell_acc.setdefault(zone, []).append(ms)
        if s.reached_billing:
            reached_billing += 1
            if s.abandoned and not s.converted:
                abandon_billing += 1

    avg_dwell_per_zone = {
        zone: round(sum(v) / len(v) / 1000, 1)  # ms -> seconds
        for zone, v in dwell_acc.items()
    }

    abandonment_rate = round(abandon_billing / reached_billing, 4) if reached_billing else 0.0
    day_orders, day_sales = _pos_today(session, store_id, win.start, win.end)

    return {
        "store_id": store_id,
        "window": {"start": win.start.isoformat(), "end": win.end.isoformat()},
        "unique_visitors": win.unique_visitors,
        "converted_visitors": win.converted_visitors,
        "conversion_rate": win.conversion_rate,
        # purchases/conversion are scoped to the observed window (see default_window);
        # the day-level POS totals are reported separately as business context.
        "purchases": win.pos_count,
        "total_basket_value_inr": round(win.pos_value_total, 2),
        "store_orders_today": day_orders,
        "store_sales_today_inr": round(day_sales, 2),
        "avg_dwell_seconds_per_zone": avg_dwell_per_zone,
        "current_queue_depth": _current_queue_depth(session, store_id, start, end),
        "billing_visitors": reached_billing,
        "abandonment_rate": abandonment_rate,
        "is_zero_traffic": win.unique_visitors == 0,
    }


def _pos_today(session: Session, store_id: str, win_start: datetime, win_end: datetime) -> tuple[int, float]:
    """All POS orders for the store on the window's day (business context, distinct from
    the window-scoped `purchases`)."""
    day_start = win_end.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start.replace(hour=23, minute=59, second=59)
    n = session.scalar(
        select(func.count()).select_from(PosRow).where(
            PosRow.store_id == store_id,
            PosRow.ts >= to_naive_utc(day_start),
            PosRow.ts <= to_naive_utc(day_end),
        )
    ) or 0
    total = session.scalar(
        select(func.coalesce(func.sum(PosRow.basket_value_inr), 0.0)).where(
            PosRow.store_id == store_id,
            PosRow.ts >= to_naive_utc(day_start),
            PosRow.ts <= to_naive_utc(day_end),
        )
    ) or 0.0
    return int(n), float(total)


def _current_queue_depth(session: Session, store_id: str, start: datetime, end: datetime) -> int:
    """Most recent observed billing queue depth in the window (0 if none)."""
    row = session.scalars(
        select(EventRow)
        .where(
            EventRow.store_id == store_id,
            EventRow.ts >= to_naive_utc(start),
            EventRow.ts < to_naive_utc(end),
            EventRow.queue_depth.is_not(None),
        )
        .order_by(EventRow.ts.desc())
        .limit(1)
    ).first()
    return int(row.queue_depth) if row and row.queue_depth is not None else 0
