"""Loads store_layout.json and answers structural questions the analytics need:
which zone is the billing zone, what zones exist, store open hours.

Loaded once and cached. If the file is missing we degrade to an empty layout
rather than crashing, and infer the billing zone by name convention.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import get_settings

# Zones whose name implies billing, used when a store omits an explicit type.
_BILLING_NAME_HINTS = ("BILLING", "CHECKOUT", "CASH", "POS", "TILL")


@lru_cache
def load_layout() -> dict:
    path = get_settings().store_layout_path
    if not path.exists():
        return {"stores": {}}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def clear_cache() -> None:
    load_layout.cache_clear()


def store_ids() -> list[str]:
    return sorted(load_layout().get("stores", {}).keys())


def store_config(store_id: str) -> dict:
    return load_layout().get("stores", {}).get(store_id, {})


def zones(store_id: str) -> list[str]:
    return sorted(store_config(store_id).get("zones", {}).keys())


def product_zones(store_id: str) -> list[str]:
    """Named product zones excluding threshold/billing (the "Zone Visit" set)."""
    out = []
    for name, cfg in store_config(store_id).get("zones", {}).items():
        ztype = (cfg or {}).get("type", "product")
        if ztype in ("product", "browse"):
            out.append(name)
    return sorted(out)


def billing_zone(store_id: str) -> str | None:
    """The zone treated as the billing/checkout area for this store."""
    cfg_zones = store_config(store_id).get("zones", {})
    for name, cfg in cfg_zones.items():
        if (cfg or {}).get("type") == "billing":
            return name
    # Fall back to a name-convention match.
    for name in cfg_zones:
        if any(h in name.upper() for h in _BILLING_NAME_HINTS):
            return name
    return None


def sku_zone(store_id: str, zone_id: str | None) -> str | None:
    if zone_id is None:
        return None
    return (store_config(store_id).get("zones", {}).get(zone_id, {}) or {}).get("sku_zone")
