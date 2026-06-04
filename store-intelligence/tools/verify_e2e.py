"""End-to-end verification on the REAL data.

Boots the API in-process on a fresh temp DB, lets startup auto-seed the real
footage-derived events (data/events.jsonl) and the real POS log
(data/pos_transactions.csv), then prints every endpoint for the real store so the
numbers can be eyeballed for logical consistency.

    python tools/verify_e2e.py
"""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp()) / 'verify.db'}"
os.environ["SEED_EVENTS"] = "1"
os.environ["SEED_POS"] = "1"
os.environ.pop("STORE_LAYOUT", None)  # use the real data/store_layout.json

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def show(c, path):
    r = c.get(path)
    print(f"\n=== GET {path}  -> {r.status_code} ===")
    print(json.dumps(r.json(), indent=2)[:1600])


with TestClient(app) as c:  # lifespan seeds real events + POS
    stores = c.get("/stores").json()["stores"]
    print("stores:", stores)
    store = "ST1008" if "ST1008" in stores else (stores[0] if stores else "ST1008")
    for ep in ("metrics", "funnel", "heatmap", "anomalies"):
        show(c, f"/stores/{store}/{ep}")
    show(c, "/health")
