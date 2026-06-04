"""Shared analytics core.

Everything downstream (metrics, funnel, heatmap, anomalies) is built from one
idea: collapse raw events into **visitor sessions**, where a session is keyed by
`visitor_id`. Because the detection layer's Re-ID assigns a *stable* token to the
same physical person across re-entries, keying on `visitor_id` is exactly what
makes re-entries not double-count (Part B requirement) and is the unit behind the
North Star conversion metric.

Staff (`is_staff=true`) are excluded from every customer-facing number here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import layout
from app.config import get_settings
from app.db import EventRow, PosRow, as_utc, to_naive_utc

# Event types that constitute "being in the billing area".
_BILLING_PRESENCE_TYPES = {"BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class VisitorSession:
    visitor_id: str
    first_seen: datetime
    last_seen: datetime
    entered: bool = False
    reentered: bool = False
    zones_visited: set[str] = field(default_factory=set)
    dwell_by_zone: dict[str, int] = field(default_factory=dict)  # max dwell_ms per zone
    reached_billing: bool = False
    billing_presence_ts: list[datetime] = field(default_factory=list)
    abandoned: bool = False
    served: bool = False          # completed billing (queue_completed, not abandoned)
    max_queue_depth: int = 0
    converted: bool = False
    min_confidence: float = 1.0


@dataclass
class StoreWindow:
    store_id: str
    start: datetime
    end: datetime
    sessions: dict[str, VisitorSession]
    pos_count: int
    pos_value_total: float

    @property
    def unique_visitors(self) -> int:
        return len(self.sessions)

    @property
    def converted_visitors(self) -> int:
        return sum(1 for s in self.sessions.values() if s.converted)

    @property
    def conversion_rate(self) -> float:
        # Zero-traffic safe: no visitors -> 0.0, never a divide-by-zero or null.
        if not self.sessions:
            return 0.0
        return round(self.converted_visitors / self.unique_visitors, 4)


def default_window(session: Session, store_id: str) -> tuple[datetime, datetime]:
    """The store's **observed operating window today**: from the first event of the
    latest day with data, to just after the freshest event.

    Anchoring to the latest event's day (not wall-clock today) means the metric always
    reflects the freshest data the store actually sent — it works for a live feed
    (events at now) and a historical replay alike, and never returns yesterday's cache.
    Scoping the *start* to the first event of that day (not midnight) keeps purchases and
    conversion on the **same basis as the observed visitors**: we only count POS
    transactions during the period we actually watched customers. This matters for short
    clips — a 2.5-minute clip's window is 2.5 minutes, so we never attribute the whole
    day's sales to a sliver of observed visitors. Falls back to the wall-clock UTC day
    when the store is empty.
    """
    latest = session.scalar(
        select(func.max(EventRow.ts)).where(EventRow.store_id == store_id)
    )
    if latest is None:
        anchor = now_utc()
        return anchor.replace(hour=0, minute=0, second=0, microsecond=0), anchor + timedelta(seconds=1)
    latest = as_utc(latest)
    day_start = latest.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest = session.scalar(
        select(func.min(EventRow.ts)).where(
            EventRow.store_id == store_id, EventRow.ts >= to_naive_utc(day_start)
        )
    )
    start = as_utc(earliest) if earliest is not None else day_start
    end = latest + timedelta(seconds=1)
    return start, end


def build_window(
    session: Session, store_id: str, start: datetime, end: datetime
) -> StoreWindow:
    """Collapse events in [start, end) into visitor sessions and run POS correlation."""
    settings = get_settings()
    bz = layout.billing_zone(store_id)
    n_start, n_end = to_naive_utc(start), to_naive_utc(end)

    rows = session.scalars(
        select(EventRow)
        .where(
            EventRow.store_id == store_id,
            EventRow.is_staff.is_(False),  # staff excluded from customer metrics
            EventRow.ts >= n_start,
            EventRow.ts < n_end,
        )
        .order_by(EventRow.ts)
    ).all()

    sessions: dict[str, VisitorSession] = {}
    product = set(layout.product_zones(store_id))
    zone_enter_ts: dict[tuple[str, str], datetime] = {}  # (visitor, zone) -> last ZONE_ENTER ts
    for r in rows:
        ts = as_utc(r.ts)
        s = sessions.get(r.visitor_id)
        if s is None:
            s = VisitorSession(visitor_id=r.visitor_id, first_seen=ts, last_seen=ts)
            sessions[r.visitor_id] = s
        s.last_seen = max(s.last_seen, ts)
        s.first_seen = min(s.first_seen, ts)
        s.min_confidence = min(s.min_confidence, r.confidence)

        et = r.event_type
        if et == "ENTRY":
            s.entered = True
        elif et == "REENTRY":
            s.entered = True
            s.reentered = True
        elif et in ("ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT") and r.zone_id:
            if r.zone_id in product:
                s.zones_visited.add(r.zone_id)
            # dwell: prefer the dwell_ms field if present (old schema / ZONE_DWELL);
            # otherwise compute it from ZONE_ENTER -> ZONE_EXIT timestamp pairs (the
            # sample's zone events carry no dwell_ms). Keep the largest seen per zone.
            if r.dwell_ms:
                s.dwell_by_zone[r.zone_id] = max(s.dwell_by_zone.get(r.zone_id, 0), r.dwell_ms)
            if et in ("ZONE_ENTER", "ZONE_DWELL"):
                zone_enter_ts[(r.visitor_id, r.zone_id)] = ts
            elif et == "ZONE_EXIT":
                t0 = zone_enter_ts.pop((r.visitor_id, r.zone_id), None)
                if t0 is not None:
                    ms = int((ts - t0).total_seconds() * 1000)
                    s.dwell_by_zone[r.zone_id] = max(s.dwell_by_zone.get(r.zone_id, 0), ms)

        # billing-area presence (zone match OR explicit queue events)
        in_billing = (bz is not None and r.zone_id == bz) or et in _BILLING_PRESENCE_TYPES
        if in_billing:
            s.reached_billing = True
            s.billing_presence_ts.append(ts)
        if et == "BILLING_QUEUE_JOIN":
            s.max_queue_depth = max(s.max_queue_depth, r.queue_depth or 0)
            # a queue_completed (mapped to JOIN) carries an explicit abandoned=False ==
            # served at billing. An old-schema BILLING_QUEUE_JOIN has no abandoned flag
            # (None), so `is False` keeps "served" to genuine completions only.
            if (r.meta or {}).get("abandoned") is False:
                s.served = True
        if et == "BILLING_QUEUE_ABANDON":
            s.abandoned = True

    # --- POS correlation: conversion ---------------------------------------
    pos_rows = session.scalars(
        select(PosRow).where(
            PosRow.store_id == store_id,
            PosRow.ts >= n_start,
            PosRow.ts < n_end,
        )
    ).all()
    txn_times = sorted(as_utc(p.ts) for p in pos_rows)
    pos_value_total = float(sum(p.basket_value_inr for p in pos_rows))

    window = timedelta(seconds=settings.conversion_window_s)
    for s in sessions.values():
        # Two convergent purchase signals (use the best available):
        # 1) POS correlation — billing presence within `window` before a real till txn
        #    (the brief's definition; primary when POS data exists, e.g. ST1008).
        # 2) "served at billing" — a queue_completed (not abandoned) event, a direct
        #    billing-outcome signal that works even for stores with no POS feed.
        if s.served:
            s.converted = True
        elif txn_times:
            for pres in s.billing_presence_ts:
                if _any_txn_in(txn_times, pres, pres + window):
                    s.converted = True
                    break

    return StoreWindow(
        store_id=store_id,
        start=start,
        end=end,
        sessions=sessions,
        pos_count=len(pos_rows),
        pos_value_total=pos_value_total,
    )


def _any_txn_in(sorted_times: list[datetime], lo: datetime, hi: datetime) -> bool:
    """Binary-search whether any txn timestamp lies in [lo, hi]."""
    import bisect

    i = bisect.bisect_left(sorted_times, lo)
    return i < len(sorted_times) and sorted_times[i] <= hi
