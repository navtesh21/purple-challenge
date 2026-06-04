"""Convert a Purplle POS export into the simple POS schema the API seeds from.

Handles both provided shapes (they're line-item level — one row per product):
* the sample export:  order_id, order_date, order_time, store_id, product_id, brand_name, total_amount
* the full export:    store_id, transaction_id, order_date, order_time, ..., total_amount (39 cols)

A *transaction* (one bill) is the set of rows sharing the same (store_id, order_date, order_time);
we collapse to one row per bill:

    store_id, transaction_id, timestamp, basket_value_inr

* timestamp      := order_date (DD-MM-YYYY) + order_time (HH:MM:SS), read as IST (store-local),
                    converted to UTC with a fixed +5:30 offset (India has no DST; the box has no tz db).
* basket_value   := sum(total_amount) over the bill's line items.
* transaction_id := the bill's invoice/order id when present, else a stable store+timestamp key.
* rows with invoice_type != 'sales' (returns) are skipped when that column exists.

Run:  python data/prepare_pos.py [path/to/pos.csv]
Writes data/pos_transactions.csv. Idempotent.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST_OFFSET = timedelta(hours=5, minutes=30)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
# default to the provided "final" sample export; fall back to the full export
DEFAULT_RAW = ROOT / "POS - sample transactionsb1e826f.csv"
FALLBACK_RAW = ROOT / "Brigade_Bangalore_10_April_26 (1)bc6219c.csv"
OUT = HERE / "pos_transactions.csv"


def _to_utc_iso(date_ddmmyyyy: str, time_hhmmss: str) -> str:
    local = datetime.strptime(f"{date_ddmmyyyy} {time_hhmmss}", "%d-%m-%Y %H:%M:%S")
    return (local - IST_OFFSET).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def convert(raw_path: Path | None = None, out_path: Path = OUT) -> dict:
    raw_path = raw_path or (DEFAULT_RAW if DEFAULT_RAW.exists() else FALLBACK_RAW)
    if not raw_path.exists():
        raise FileNotFoundError(f"POS export not found: {raw_path}")

    value: dict = defaultdict(float)
    meta: dict = {}
    with open(raw_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            store = (r.get("store_id") or "").strip()
            date = (r.get("order_date") or "").strip()
            time = (r.get("order_time") or "").strip()
            if not (store and date and time):
                continue
            if (r.get("invoice_type") or "sales").strip().lower() != "sales":
                continue  # skip returns when the column exists
            key = (store, date, time)                       # one bill = same store + timestamp
            try:
                value[key] += float(r.get("total_amount") or 0)
            except ValueError:
                pass
            meta.setdefault(key, {
                # prefer a real bill id; the sample's order_id is a row counter, so fall
                # back to a stable store+timestamp key shared by the bill's line items.
                "txn": (r.get("transaction_id") or r.get("invoice_number") or "").strip(),
            })

    rows = []
    for (store, date, time), m in meta.items():
        txn = m["txn"] or f"{store}_{date}_{time}".replace(" ", "")
        rows.append({
            "store_id": store,
            "transaction_id": txn,
            "timestamp": _to_utc_iso(date, time),
            "basket_value_inr": f"{round(value[(store, date, time)], 2):.2f}",
        })
    rows.sort(key=lambda x: x["timestamp"])

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"])
        w.writeheader()
        w.writerows(rows)

    return {
        "raw": str(raw_path), "out": str(out_path), "orders": len(rows),
        "stores": sorted({r["store_id"] for r in rows}),
        "first": rows[0]["timestamp"] if rows else None,
        "last": rows[-1]["timestamp"] if rows else None,
    }


if __name__ == "__main__":
    import json
    raw = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(convert(raw), indent=2))
