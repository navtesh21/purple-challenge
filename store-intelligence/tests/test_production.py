# PROMPT: "Write pytest cases for the production-readiness behaviours: a DB outage must
#          return HTTP 503 with a structured body and no stack trace; /health must report
#          degraded when the DB is down; POS data must seed from CSV on startup; the
#          dashboard and store list must be served."
# CHANGES MADE: Used FastAPI dependency_overrides to inject the DB outage (cleaner and
#   faster than pointing at a dead Postgres, which would also break lifespan startup);
#   monkeypatched the health ping so /health degrades without tearing the engine down.

import csv
from datetime import timedelta

from app.db import DBUnavailable, get_session, init_db, reset_engine
from tests._helpers import DAY, STORE, billing_time, mk, visitor


def test_db_outage_returns_structured_503(client):
    from app.main import app

    def boom():
        raise DBUnavailable("connection refused")
        yield  # generator dependency; never reached

    app.dependency_overrides[get_session] = boom
    try:
        r = client.get(f"/stores/{STORE}/metrics")
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "database_unavailable"
        assert "trace_id" in body
        # no raw stack trace leaks to the client
        assert "Traceback" not in r.text
    finally:
        app.dependency_overrides.clear()


def test_health_degraded_when_db_down(client, monkeypatch):
    monkeypatch.setattr("app.health.ping", lambda: False)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
    assert "DATABASE_UNAVAILABLE" in body["warnings"]


def test_dashboard_and_store_list_served(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "Store Intelligence" in home.text
    stores = client.get("/stores").json()["stores"]
    assert "STORE_BLR_002" in stores


def test_persist_survives_concurrent_duplicate(client):
    # Simulate the check-then-insert race: a row sneaks in between the idempotency
    # SELECT and our INSERT. _persist must roll back the bulk insert and re-insert
    # row-by-row, counting the collision as a duplicate (idempotent), not 500.
    from app.ingestion import _persist, _row_from_event
    from app.models import Event

    def row(vid, eid):
        ev = Event.model_validate(mk("ENTRY", vid=vid, camera="CAM_ENTRY_01"))
        r = _row_from_event(ev)
        r.event_id = eid
        return r

    gen = get_session()
    session = next(gen)
    try:
        # a concurrent writer already inserted event_id "dup"
        session.add(row("VIS_a", "11111111-1111-4111-8111-111111111111"))
        session.commit()
        # our batch unknowingly includes that same id plus a fresh one
        new_rows = [
            row("VIS_a", "11111111-1111-4111-8111-111111111111"),
            row("VIS_b", "22222222-2222-4222-8222-222222222222"),
        ]
        accepted, extra_dups = _persist(session, new_rows)
        assert accepted == 1
        assert extra_dups == 1
    finally:
        gen.close()


def test_pos_seeded_from_csv_on_startup(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import layout as layout_mod
    from app.config import get_settings

    # POS within the visitor's own event span (their EXIT is ~30s after billing), so the
    # default window (anchored to the freshest event) includes the transaction.
    presence = billing_time(DAY) + timedelta(seconds=20)
    csv_path = tmp_path / "pos.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["store_id", "transaction_id", "timestamp", "basket_value_inr"])
        w.writerow([STORE, "TXN_SEED", presence.strftime("%Y-%m-%dT%H:%M:%SZ"), "1500.00"])

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'seed.db'}")
    monkeypatch.setenv("SEED_POS", "1")
    monkeypatch.setenv("POS_CSV", str(csv_path))
    get_settings.cache_clear()
    layout_mod.clear_cache()
    reset_engine()
    init_db()

    from app.main import app

    with TestClient(app) as c:  # lifespan seeds POS from the CSV
        c.post("/events/ingest", json={"events": visitor("VIS_seed", t0=DAY, billing=True)})
        m = c.get(f"/stores/{STORE}/metrics").json()
        assert m["purchases"] == 1            # the seeded txn is counted
        assert m["converted_visitors"] == 1   # and correlates to the billing visitor
    reset_engine()
