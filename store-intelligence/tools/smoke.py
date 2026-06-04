"""Quick in-process smoke test: spin the app on a temp SQLite DB, ingest a slice
of the sample events, and print the headline endpoints. Not a substitute for the
pytest suite — just a fast manual sanity check during development."""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tmp) / 'smoke.db'}"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

events = [json.loads(l) for l in (ROOT / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()[:5000]]
store = events[0]["store_id"]

with TestClient(app) as c:
    # ingest in batches of 500
    acc = dup = rej = 0
    for i in range(0, len(events), 500):
        r = c.post("/events/ingest", json={"events": events[i:i + 500]})
        b = r.json()
        acc += b["accepted"]; dup += b["duplicates"]; rej += b["rejected"]
    print("INGEST:", {"accepted": acc, "duplicates": dup, "rejected": rej})

    # idempotency: re-send the first batch
    r = c.post("/events/ingest", json={"events": events[:500]})
    print("RE-INGEST (idempotent):", r.json())

    for ep in ("metrics", "funnel", "heatmap", "anomalies"):
        r = c.get(f"/stores/{store}/{ep}")
        print(f"\n=== {ep} ({store}) ===")
        print(json.dumps(r.json(), indent=2)[:1200])

    print("\n=== health ===")
    print(json.dumps(c.get("/health").json(), indent=2)[:800])
