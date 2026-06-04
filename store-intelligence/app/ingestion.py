"""Event ingestion: validate, deduplicate, persist — idempotently.

Contract (Part B / Part C):
* batches of up to `ingest_max_batch` events
* per-item validation -> partial success (one bad event never sinks the batch)
* idempotent by event_id: re-sending the same payload persists nothing new and
  returns the same shape, counting repeats as `duplicates`
* structured error response listing each rejected item by index
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import EventRow, to_naive_utc
from app.models import Event, IngestItemError, IngestResponse
from app.schema_compat import normalize_event


def _row_from_event(ev: Event) -> EventRow:
    return EventRow(
        event_id=ev.event_id,
        store_id=ev.store_id,
        camera_id=ev.camera_id,
        visitor_id=ev.visitor_id,
        event_type=ev.event_type,  # use_enum_values -> already a str
        ts=to_naive_utc(ev.timestamp),
        zone_id=ev.zone_id,
        dwell_ms=ev.dwell_ms,
        is_staff=ev.is_staff,
        confidence=ev.confidence,
        queue_depth=ev.metadata.queue_depth,
        sku_zone=ev.metadata.sku_zone,
        session_seq=ev.metadata.session_seq,
        meta=ev.metadata.model_dump(),
        ingested_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _short_error(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors()[:3]:
        loc = ".".join(str(p) for p in e["loc"])
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


def ingest_events(session: Session, raw_events: list[dict]) -> IngestResponse:
    settings = get_settings()
    received = len(raw_events)
    errors: list[IngestItemError] = []

    if received > settings.ingest_max_batch:
        # Reject the whole over-size batch loudly rather than silently truncating.
        raise ValueError(
            f"batch size {received} exceeds maximum {settings.ingest_max_batch}"
        )

    # 1. validate per item (partial success)
    valid: dict[str, Event] = {}
    in_batch_dups = 0
    for idx, raw in enumerate(raw_events):
        # normalize any accepted wire shape (sample's 3 families, our pipeline output,
        # or the older canonical shape) into the one dict Event validates.
        try:
            ev = Event.model_validate(normalize_event(raw))
        except ValidationError as exc:
            errors.append(
                IngestItemError(
                    index=idx,
                    event_id=(raw.get("event_id") or raw.get("queue_event_id")) if isinstance(raw, dict) else None,
                    error=_short_error(exc),
                )
            )
            continue
        except (ValueError, TypeError) as exc:
            errors.append(
                IngestItemError(
                    index=idx,
                    event_id=(raw.get("event_id") or raw.get("queue_event_id")) if isinstance(raw, dict) else None,
                    error=str(exc),
                )
            )
            continue
        # 2. in-batch dedup by event_id (first occurrence wins). A repeat within the
        # same payload counts as a duplicate so received == accepted+duplicates+rejected.
        if ev.event_id in valid:
            in_batch_dups += 1
        else:
            valid[ev.event_id] = ev

    # 3. idempotency: skip event_ids already persisted
    duplicates = in_batch_dups
    accepted = 0
    if valid:
        ids = list(valid.keys())
        existing: set[str] = set()
        # chunk the IN() query so very large batches stay within driver limits
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            existing.update(
                session.scalars(
                    select(EventRow.event_id).where(EventRow.event_id.in_(chunk))
                ).all()
            )
        duplicates += len(existing)
        new_rows = [_row_from_event(ev) for eid, ev in valid.items() if eid not in existing]
        if new_rows:
            accepted, extra_dups = _persist(session, new_rows)
            duplicates += extra_dups

    # Defensive invariant: every received event is accounted for exactly once.
    assert received == accepted + duplicates + len(errors), (
        f"ingest accounting drift: {received} != {accepted}+{duplicates}+{len(errors)}"
    )
    return IngestResponse(
        received=received,
        accepted=accepted,
        duplicates=duplicates,
        rejected=len(errors),
        errors=errors,
    )


def _persist(session: Session, new_rows: list[EventRow]) -> tuple[int, int]:
    """Insert new rows, surviving the check-then-insert race.

    The idempotency SELECT and this INSERT are not one atomic step, so a concurrent
    request carrying the same event_id can insert between them. The bulk insert then
    trips the event_id primary-key constraint. Rather than 500, we roll back and
    re-insert one row at a time: rows that now collide are counted as duplicates
    (someone else persisted them — still idempotent), the rest are accepted. Returns
    (accepted, extra_duplicates)."""
    try:
        session.add_all(new_rows)
        session.commit()
        return len(new_rows), 0
    except IntegrityError:
        session.rollback()

    accepted = 0
    dups = 0
    for row in new_rows:
        try:
            session.add(row)
            session.commit()
            accepted += 1
        except IntegrityError:
            session.rollback()  # a concurrent writer already inserted this event_id
            dups += 1
    return accepted, dups
