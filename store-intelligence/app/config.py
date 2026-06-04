"""Central configuration. Everything tunable lives here and is overridable by env var,
so the same image runs in compose (Postgres) and in tests (SQLite) with no code change.

Env vars are read in ``__init__`` (NOT as class-body defaults) so that a process which
changes an env var and clears the cache — e.g. the test suite swapping DATABASE_URL per
test — actually picks up the new value. Reading them at class-definition time would bind
them once at import and silently ignore later changes.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# Repo root = parent of the app/ package.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


class Settings:
    def __init__(self) -> None:
        # --- storage -----------------------------------------------------
        # Default to a local SQLite file so `pytest` and `uvicorn` work with zero
        # infra. docker-compose overrides DATABASE_URL to point at Postgres.
        self.database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'store_intel.db'}")

        # --- dataset -----------------------------------------------------
        self.store_layout_path: Path = Path(os.getenv("STORE_LAYOUT", str(DATA_DIR / "store_layout.json")))
        self.pos_csv_path: Path = Path(os.getenv("POS_CSV", str(DATA_DIR / "pos_transactions.csv")))
        # Seed POS rows into the DB on startup when the table is empty.
        self.seed_pos_on_start: bool = os.getenv("SEED_POS", "1") == "1"
        # Seed the committed, footage-derived events on startup when the events table is
        # empty, so `docker compose up` alone yields a populated /metrics (no manual
        # replay step). The same JSONL can also be replayed live via tools/simulate.py.
        self.events_jsonl_path: Path = Path(os.getenv("EVENTS_JSONL", str(DATA_DIR / "events.jsonl")))
        self.seed_events_on_start: bool = os.getenv("SEED_EVENTS", "1") == "1"

        # --- business rules (every threshold is named, never a magic literal) ---
        self.ingest_max_batch: int = int(os.getenv("INGEST_MAX_BATCH", "500"))
        # A visitor in the billing zone within this window before a POS txn counts
        # as the converter for that transaction.
        self.conversion_window_s: int = int(os.getenv("CONVERSION_WINDOW_S", "300"))
        # Heatmap / metrics flag low confidence below this many sessions in the window.
        self.min_sessions_for_confidence: int = int(os.getenv("MIN_SESSIONS_CONF", "20"))
        # health: feed considered stale past this lag.
        self.stale_feed_s: int = int(os.getenv("STALE_FEED_S", "600"))
        # anomalies
        self.queue_spike_warn: int = int(os.getenv("QUEUE_SPIKE_WARN", "5"))
        self.queue_spike_critical: int = int(os.getenv("QUEUE_SPIKE_CRITICAL", "8"))
        self.dead_zone_s: int = int(os.getenv("DEAD_ZONE_S", "1800"))
        self.conversion_drop_warn: float = float(os.getenv("CONV_DROP_WARN", "0.20"))      # 20% relative
        self.conversion_drop_critical: float = float(os.getenv("CONV_DROP_CRIT", "0.40"))  # 40% relative
        self.anomaly_baseline_days: int = int(os.getenv("ANOMALY_BASELINE_DAYS", "7"))

        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
