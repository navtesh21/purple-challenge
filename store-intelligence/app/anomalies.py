"""GET /stores/{id}/anomalies — active operational anomalies.

Three detectors, each returning a severity and a concrete suggested_action an
operator can act on:

* BILLING_QUEUE_SPIKE  — current queue depth past WARN/CRITICAL thresholds
* CONVERSION_DROP      — today's conversion vs the trailing N-day baseline
* DEAD_ZONE            — a product zone with no visits for 30+ min during open hours

"Now" is anchored to the store's freshest event, so the detectors behave
identically on a live feed and on a historical replay.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import layout
from app.analytics import build_window, default_window, now_utc
from app.config import get_settings
from app.db import EventRow, as_utc, to_naive_utc

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _store_now(session: Session, store_id: str) -> datetime:
    latest = session.scalar(
        select(func.max(EventRow.ts)).where(EventRow.store_id == store_id)
    )
    return as_utc(latest) if latest is not None else now_utc()


def _is_open(store_id: str, ref: datetime) -> bool:
    cfg = layout.store_config(store_id).get("open_hours")
    if not cfg:
        return True
    tz = cfg.get("tz", "UTC")
    try:
        local = ref.astimezone(ZoneInfo(tz)) if ZoneInfo else ref
    except Exception:
        local = ref
    o = cfg.get("open", "00:00")
    c = cfg.get("close", "23:59")
    hhmm = local.strftime("%H:%M")
    return o <= hhmm <= c


def detect_anomalies(session: Session, store_id: str) -> dict:
    settings = get_settings()
    ref = _store_now(session, store_id)
    anomalies: list[dict] = []

    anomalies += _queue_spike(session, store_id, ref, settings)
    cd = _conversion_drop(session, store_id, settings)
    if cd:
        anomalies.append(cd)
    anomalies += _dead_zones(session, store_id, ref, settings)

    # surface the most severe first
    order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    anomalies.sort(key=lambda a: order.get(a["severity"], 9))
    return {
        "store_id": store_id,
        "as_of": ref.isoformat(),
        "active_count": len(anomalies),
        "anomalies": anomalies,
    }


def _queue_spike(session, store_id, ref, settings) -> list[dict]:
    # the most recent queue depth observed in the last 10 minutes
    cutoff = to_naive_utc(ref - timedelta(seconds=600))
    row = session.scalars(
        select(EventRow)
        .where(
            EventRow.store_id == store_id,
            EventRow.queue_depth.is_not(None),
            EventRow.ts >= cutoff,
        )
        .order_by(EventRow.ts.desc())
        .limit(1)
    ).first()
    if not row or row.queue_depth is None:
        return []
    depth = int(row.queue_depth)
    if depth >= settings.queue_spike_critical:
        sev = "CRITICAL"
    elif depth >= settings.queue_spike_warn:
        sev = "WARN"
    else:
        return []
    return [
        {
            "type": "BILLING_QUEUE_SPIKE",
            "severity": sev,
            "metric": {"queue_depth": depth, "observed_at": as_utc(row.ts).isoformat()},
            "message": f"Billing queue depth is {depth}.",
            "suggested_action": "Open an additional till or redirect a floor associate to billing.",
        }
    ]


def _conversion_drop(session, store_id, settings) -> dict | None:
    start, end = default_window(session, store_id)
    today = build_window(session, store_id, start, end)
    if today.unique_visitors == 0:
        return None

    # trailing baseline: the N days before today's window
    rates: list[float] = []
    for d in range(1, settings.anomaly_baseline_days + 1):
        b_start = start - timedelta(days=d)
        b_end = start - timedelta(days=d - 1)
        bw = build_window(session, store_id, b_start, b_end)
        if bw.unique_visitors > 0:
            rates.append(bw.conversion_rate)

    if len(rates) < 2:  # not enough history to call a "drop"
        return None
    baseline = sum(rates) / len(rates)
    if baseline <= 0:
        return None

    rel_drop = (baseline - today.conversion_rate) / baseline
    if rel_drop < settings.conversion_drop_warn:
        return None
    sev = "CRITICAL" if rel_drop >= settings.conversion_drop_critical else "WARN"
    return {
        "type": "CONVERSION_DROP",
        "severity": sev,
        "metric": {
            "today": today.conversion_rate,
            "baseline_avg": round(baseline, 4),
            "relative_drop": round(rel_drop, 4),
            "baseline_days": len(rates),
        },
        "message": (
            f"Conversion {today.conversion_rate:.0%} is {rel_drop:.0%} below the "
            f"{len(rates)}-day average of {baseline:.0%}."
        ),
        "suggested_action": "Check staffing on the floor and at billing; verify no camera feed is stale.",
    }


def _dead_zones(session, store_id, ref, settings) -> list[dict]:
    if not _is_open(store_id, ref):
        return []
    # only meaningful once the store has had some traffic today
    start, _end = default_window(session, store_id)
    had_traffic = session.scalar(
        select(func.count())
        .select_from(EventRow)
        .where(EventRow.store_id == store_id, EventRow.ts >= to_naive_utc(start))
    )
    if not had_traffic:
        return []

    out = []
    cutoff = ref - timedelta(seconds=settings.dead_zone_s)
    for zone in layout.product_zones(store_id):
        last = session.scalar(
            select(func.max(EventRow.ts)).where(
                EventRow.store_id == store_id,
                EventRow.zone_id == zone,
                EventRow.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            )
        )
        last_utc = as_utc(last)
        if last_utc is None or last_utc < cutoff:
            mins = settings.dead_zone_s // 60
            out.append(
                {
                    "type": "DEAD_ZONE",
                    "severity": "INFO",
                    "metric": {
                        "zone_id": zone,
                        "last_visit": last_utc.isoformat() if last_utc else None,
                    },
                    "message": f"No visits to {zone} in the last {mins} min.",
                    "suggested_action": f"Check {zone} for blocked access or a misaimed camera; consider a promotion.",
                }
            )
    return out
