"""FastAPI entrypoint for the Store Intelligence API.

Wires the routes, the structured-logging middleware, startup (create tables, seed
POS), the live dashboard, and the exception handlers that turn internal failures
into structured HTTP responses (503 on DB loss, 400 on bad batch) with no raw
stack traces leaking to clients (Part C: graceful degradation).
"""
from __future__ import annotations

import csv
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

log = logging.getLogger("store_intel.startup")

from app import anomalies as anomalies_mod
from app import funnel as funnel_mod
from app import heatmap as heatmap_mod
from app import metrics as metrics_mod
from app.config import ROOT, get_settings
from app.db import DBUnavailable, PosRow, get_session, init_db
from app.health import compute_health
from app.ingestion import ingest_events
from app.logging_mw import StructuredLoggingMiddleware, configure_logging
from app.models import IngestRequest, IngestResponse

DASHBOARD_DIR = ROOT / "dashboard"


def _seed_pos(settings) -> None:
    """Load pos_transactions.csv into the DB once, if the table is empty.

    Conversion correlation needs POS data; seeding here means a fresh container
    has a working /metrics conversion number with no manual import step."""
    if not settings.seed_pos_on_start or not settings.pos_csv_path.exists():
        return
    gen = get_session()
    session: Session = next(gen)
    try:
        existing = session.scalar(select(func.count()).select_from(PosRow))
        if existing:
            return
        rows = []
        with open(settings.pos_csv_path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                rows.append(
                    PosRow(
                        transaction_id=r["transaction_id"].strip(),
                        store_id=r["store_id"].strip(),
                        ts=ts.astimezone(timezone.utc).replace(tzinfo=None),
                        basket_value_inr=float(r["basket_value_inr"]),
                    )
                )
        if rows:
            session.add_all(rows)
            session.commit()
            log.info("seeded %d POS rows from %s", len(rows), settings.pos_csv_path.name)
    except Exception as exc:  # noqa: BLE001 - seeding is optional; never crash startup
        # A malformed POS CSV must not take the whole API down. Log and continue with
        # an empty POS table (conversion simply reads as 0 until events/POS arrive).
        log.warning("POS seeding skipped: %s", exc)
    finally:
        gen.close()


def _seed_events(settings) -> None:
    """Ingest the committed, footage-derived events once, if the events table is empty.

    This is what makes `docker compose up` self-sufficient: /metrics returns real
    numbers immediately, with no manual replay. Events go through the same validated
    ingest path as a live POST, so seeding can't introduce schema-invalid rows."""
    if not settings.seed_events_on_start or not settings.events_jsonl_path.exists():
        return
    gen = get_session()
    session: Session = next(gen)
    try:
        from app.db import EventRow
        from app.ingestion import ingest_events

        if session.scalar(select(func.count()).select_from(EventRow)):
            return
        raw = []
        with open(settings.events_jsonl_path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    raw.append(__import__("json").loads(line))
        total = 0
        for i in range(0, len(raw), settings.ingest_max_batch):
            res = ingest_events(session, raw[i : i + settings.ingest_max_batch])
            total += res.accepted
        if total:
            log.info("seeded %d events from %s", total, settings.events_jsonl_path.name)
    except Exception as exc:  # noqa: BLE001 - seeding is optional; never crash startup
        log.warning("event seeding skipped: %s", exc)
    finally:
        gen.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    _seed_pos(get_settings())
    _seed_events(get_settings())
    yield


app = FastAPI(
    title="Apex Retail — Store Intelligence API",
    version="1.0.0",
    description="Offline store analytics from CCTV-derived behavioural events.",
    lifespan=lifespan,
)
app.add_middleware(StructuredLoggingMiddleware)


# --- exception handlers: structured, no stack traces ------------------------


@app.exception_handler(DBUnavailable)
async def _db_unavailable(request: Request, exc: DBUnavailable):
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "detail": "The analytics datastore is temporarily unreachable.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(ValueError)
async def _bad_request(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=400,
        content={
            "error": "bad_request",
            "detail": str(exc),
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(SQLAlchemyError)
async def _db_error(request: Request, exc: SQLAlchemyError):
    # Any database error that isn't already a clean DBUnavailable becomes a structured
    # 503 — the datastore is misbehaving, which is a dependency failure, not a bug to
    # leak. No stack trace reaches the client.
    log.warning("db error on %s: %s", request.url.path, type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_error",
            "detail": "The analytics datastore returned an error.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Last-resort catch-all (Part C: no raw stack traces in responses). The full
    # error is logged server-side with the trace_id for debugging; the client gets a
    # structured 500 only.
    log.exception("unhandled error on %s [trace_id=%s]", request.url.path,
                  getattr(request.state, "trace_id", None))
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": "An unexpected error occurred.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


# --- routes -----------------------------------------------------------------


@app.post("/events/ingest", response_model=IngestResponse)
def ingest(payload: IngestRequest, request: Request, session: Session = Depends(get_session)):
    request.state.event_count = len(payload.events)
    return ingest_events(session, payload.events)


@app.get("/stores/{store_id}/metrics")
def get_metrics(store_id: str, session: Session = Depends(get_session)):
    return metrics_mod.compute_metrics(session, store_id)


@app.get("/stores/{store_id}/funnel")
def get_funnel(store_id: str, session: Session = Depends(get_session)):
    return funnel_mod.compute_funnel(session, store_id)


@app.get("/stores/{store_id}/heatmap")
def get_heatmap(store_id: str, session: Session = Depends(get_session)):
    return heatmap_mod.compute_heatmap(session, store_id)


@app.get("/stores/{store_id}/anomalies")
def get_anomalies(store_id: str, session: Session = Depends(get_session)):
    return anomalies_mod.detect_anomalies(session, store_id)


@app.get("/health")
def health():
    return compute_health()


@app.get("/stores")
def list_stores(session: Session = Depends(get_session)):
    from app import layout

    seen = session.scalars(select(func.distinct(PosRow.store_id))).all()
    known = sorted(set(layout.store_ids()) | set(seen))
    return {"stores": known}


# --- live dashboard (Part E) ------------------------------------------------

if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

    @app.get("/")
    def dashboard():
        index = DASHBOARD_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"service": "store-intelligence", "docs": "/docs"})

else:  # pragma: no cover

    @app.get("/")
    def root():
        return {"service": "store-intelligence", "docs": "/docs"}
