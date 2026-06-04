"""Persistence layer.

Design notes:
* SQLAlchemy 2.0 ORM with only portable column types, so the identical models run
  on Postgres (compose / production) and SQLite (tests, local dev).
* Timestamps are stored as tz-naive UTC. Postgres and SQLite disagree on tz-aware
  handling, so we normalise to naive-UTC on write and re-attach UTC on read. All
  comparisons in the codebase are therefore in one tz and never raise the
  "can't compare offset-naive and offset-aware" error.
* `DBUnavailable` is raised when the engine can't be reached; the API maps it to a
  structured HTTP 503 (Part C: graceful degradation, no stack traces in responses).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from app.config import get_settings


class DBUnavailable(RuntimeError):
    """Raised when the database cannot be reached. Mapped to HTTP 503."""


def to_naive_utc(dt: datetime) -> datetime:
    """Coerce any datetime to tz-naive UTC for storage/comparison."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc(dt: datetime | None) -> datetime | None:
    """Re-attach UTC tzinfo to a naive datetime read back from the DB."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "events"

    # event_id is the natural key. Re-ingesting the same id is an idempotent no-op.
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(32), index=True)
    camera_id: Mapped[str] = mapped_column(String(32))
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dwell_ms: Mapped[int] = mapped_column(Integer, default=0)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    queue_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sku_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)


class PosRow(Base):
    __tablename__ = "pos_transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    basket_value_inr: Mapped[float] = mapped_column(Float, default=0.0)


# --- engine / session management -------------------------------------------

_engine = None
_SessionLocal: sessionmaker | None = None


def _build_engine():
    settings = get_settings()
    url = settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    # pool_pre_ping turns a dropped Postgres connection into a clean reconnect
    # attempt instead of a stale-socket error mid-request.
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, future=True)


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _build_engine()
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """Create tables if absent. Safe to call repeatedly (startup)."""
    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """Drop cached engine/session (used by tests that swap DATABASE_URL)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_session():
    """FastAPI dependency. Yields a session and converts connection failures into
    DBUnavailable so the route layer can return a structured 503."""
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
    except OperationalError as exc:  # DB unreachable / dropped mid-request
        session.rollback()
        raise DBUnavailable(str(exc)) from exc
    finally:
        session.close()


def ping() -> bool:
    """True if the DB answers a trivial query. Used by /health."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
