"""Verify both stores end-to-end on the combined wire-format events + POS."""
import json, os, tempfile
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/v.db"
os.environ.pop("STORE_LAYOUT", None)
os.environ["SEED_EVENTS"] = "1"; os.environ["SEED_POS"] = "1"
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as c:
    stores = c.get("/stores").json()["stores"]
    print("stores:", stores)
    for s in stores:
        m = c.get(f"/stores/{s}/metrics").json()
        f = c.get(f"/stores/{s}/funnel").json()
        h = c.get(f"/stores/{s}/heatmap").json()
        a = c.get(f"/stores/{s}/anomalies").json()
        fn = " ".join(f"{st['stage']}={st['count']}" for st in f["stages"])
        print(f"\n{s}: visitors={m['unique_visitors']} conv={m['conversion_rate']} "
              f"served/billing={m['billing_visitors']} purchases={m['purchases']} orders_today={m['store_orders_today']}")
        print(f"   funnel: {fn}")
        print(f"   zones : {[(c2['zone_id'], c2['visits']) for c2 in h['cells']]}  conf={h['data_confidence']}")
        print(f"   anomalies: {a['active_count']}")
    print("\nhealth:", c.get("/health").json()["status"])
