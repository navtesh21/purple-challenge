"""GET /stores/{id}/heatmap — per-zone visit frequency + avg dwell, normalised
0-100 and ready to drop straight into a grid renderer.

`data_confidence` is LOW when the window holds fewer than the configured number of
sessions, so the dashboard can grey-out a heatmap built on too little data instead
of presenting noise as signal.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app import layout
from app.analytics import build_window, default_window
from app.config import get_settings


def compute_heatmap(
    session: Session, store_id: str, start: datetime | None = None, end: datetime | None = None
) -> dict:
    if start is None or end is None:
        start, end = default_window(session, store_id)
    win = build_window(session, store_id, start, end)
    settings = get_settings()

    visits: dict[str, int] = {}
    dwell_sum: dict[str, int] = {}
    dwell_n: dict[str, int] = {}

    for s in win.sessions.values():
        for zone in s.zones_visited:
            visits[zone] = visits.get(zone, 0) + 1
        for zone, ms in s.dwell_by_zone.items():
            dwell_sum[zone] = dwell_sum.get(zone, 0) + ms
            dwell_n[zone] = dwell_n.get(zone, 0) + 1

    # the heatmap grid is product zones only (billing/threshold are not "attention"
    # zones); include every product zone even at zero traffic for a stable grid
    product = set(layout.product_zones(store_id))
    all_zones = sorted(product | (set(visits) & product))
    avg_dwell = {z: (dwell_sum[z] / dwell_n[z] / 1000) for z in dwell_sum}  # seconds
    # normalise intensities against the product-zone maxima only
    max_visits = max((visits.get(z, 0) for z in all_zones), default=0)
    max_dwell = max((avg_dwell.get(z, 0.0) for z in all_zones), default=0.0)

    cells = []
    for z in all_zones:
        v = visits.get(z, 0)
        d = avg_dwell.get(z, 0.0)
        cells.append(
            {
                "zone_id": z,
                "sku_zone": layout.sku_zone(store_id, z),
                "visits": v,
                "avg_dwell_seconds": round(d, 1),
                "visit_intensity": round(100 * v / max_visits, 1) if max_visits else 0.0,
                "dwell_intensity": round(100 * d / max_dwell, 1) if max_dwell else 0.0,
            }
        )

    n_sessions = win.unique_visitors
    return {
        "store_id": store_id,
        "window": {"start": win.start.isoformat(), "end": win.end.isoformat()},
        "sessions": n_sessions,
        "data_confidence": "LOW" if n_sessions < settings.min_sessions_for_confidence else "OK",
        "min_sessions_for_confidence": settings.min_sessions_for_confidence,
        "cells": cells,
    }
