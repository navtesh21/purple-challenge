"""GET /health — the first thing an on-call engineer checks.

Reports service + DB status and, per store, the last event timestamp and feed lag.
A feed lagging more than the configured threshold is flagged STALE_FEED. Lag is
measured against wall-clock now (an honest health check: historical data really is
stale; a live feed is fresh).

This endpoint is intentionally defensive — if the DB is down it still returns a
structured body (status=degraded) rather than throwing, because that is exactly
when someone is reading it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app import layout
from app.config import get_settings
from app.db import EventRow, as_utc, get_session, ping


def compute_health() -> dict:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    db_up = ping()

    if not db_up:
        return {
            "status": "degraded",
            "db": "down",
            "now": now.isoformat(),
            "stores": [],
            "warnings": ["DATABASE_UNAVAILABLE"],
        }

    stores_status = []
    warnings = []
    # union of stores known from layout and stores seen in events
    gen = get_session()
    session = next(gen)
    try:
        seen = session.scalars(select(EventRow.store_id).distinct()).all()
        known = sorted(set(layout.store_ids()) | set(seen))
        for sid in known:
            last = session.scalar(
                select(func.max(EventRow.ts)).where(EventRow.store_id == sid)
            )
            last_utc = as_utc(last)
            if last_utc is None:
                stores_status.append(
                    {"store_id": sid, "last_event_at": None, "lag_seconds": None, "status": "NO_DATA"}
                )
                continue
            lag = (now - last_utc).total_seconds()
            stale = lag > settings.stale_feed_s
            if stale:
                warnings.append(f"STALE_FEED:{sid}")
            stores_status.append(
                {
                    "store_id": sid,
                    "last_event_at": last_utc.isoformat(),
                    "lag_seconds": round(lag, 1),
                    "status": "STALE_FEED" if stale else "OK",
                }
            )
    finally:
        gen.close()

    any_stale = any(s["status"] == "STALE_FEED" for s in stores_status)
    return {
        "status": "ok" if not any_stale else "warning",
        "db": "up",
        "now": now.isoformat(),
        "stale_feed_threshold_seconds": settings.stale_feed_s,
        "stores": stores_status,
        "warnings": warnings,
    }
