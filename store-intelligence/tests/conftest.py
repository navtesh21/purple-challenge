"""Shared test fixtures.

Each test gets a fresh, isolated SQLite database (a temp file) so cases never leak
state into one another. POS seeding from the real CSV is turned OFF here — tests
that need POS insert exactly the rows they assert on, via the `add_pos` helper, so
every conversion number in a test is fully controlled.

Env vars are set BEFORE importing the app, then the settings cache and DB engine
are reset, so the app binds to the test database.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone

import pytest

# point the app at a throwaway DB and a fixture layout; disable ALL startup seeding so
# tests control their own data and never touch the real data/ files.
_TMP = tempfile.mkdtemp()
os.environ["SEED_POS"] = "0"
os.environ["SEED_EVENTS"] = "0"

from app import layout as layout_mod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import PosRow, get_session, init_db, reset_engine  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(__file__))
# Tests run against a fixed multi-store fixture layout, NOT the production
# data/store_layout.json (which carries the real Brigade Road store). This keeps the
# suite stable no matter what real data ships.
os.environ["STORE_LAYOUT"] = os.path.join(ROOT, "tests", "fixtures", "store_layout.json")


@pytest.fixture()
def client():
    """A TestClient bound to a fresh SQLite DB for this test."""
    from fastapi.testclient import TestClient

    db_path = os.path.join(_TMP, f"t_{uuid.uuid4().hex}.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    get_settings.cache_clear()
    layout_mod.clear_cache()
    reset_engine()
    init_db()

    from app.main import app

    with TestClient(app) as c:
        yield c
    reset_engine()


@pytest.fixture()
def add_pos():
    """Insert a POS transaction directly into the test DB."""
    def _add(store_id: str, ts: datetime, value: float, txn_id: str | None = None):
        gen = get_session()
        session = next(gen)
        try:
            session.add(
                PosRow(
                    transaction_id=txn_id or "TXN_" + uuid.uuid4().hex[:8],
                    store_id=store_id,
                    ts=ts.astimezone(timezone.utc).replace(tzinfo=None),
                    basket_value_inr=value,
                )
            )
            session.commit()
        finally:
            gen.close()

    return _add
