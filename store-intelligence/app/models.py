"""Canonical event schema (Part A output contract / Part B input contract).

This single module is imported by both the detection pipeline (to emit) and the
API (to validate on ingest), so producer and consumer can never drift apart.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Event types that are threshold crossings, not zone events: their zone_id must be null.
_THRESHOLD_TYPES = {"ENTRY", "EXIT", "REENTRY"}


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    # Extra fields are tolerated: the pipeline may attach diagnostic keys and we
    # would rather store them than reject an otherwise-valid event.
    model_config = ConfigDict(extra="allow")

    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None


class Event(BaseModel):
    """One behavioural event. Mirrors the required output schema exactly."""

    model_config = ConfigDict(use_enum_values=True)

    event_id: str = Field(..., min_length=1, description="globally unique, uuid-v4")
    store_id: str = Field(..., min_length=1)
    camera_id: str = Field(..., min_length=1)
    visitor_id: str = Field(..., min_length=1, description="Re-ID token, stable across re-entry")
    event_type: EventType
    timestamp: datetime = Field(..., description="ISO-8601 UTC")
    zone_id: Optional[str] = Field(None, description="null for ENTRY / EXIT")
    dwell_ms: int = Field(0, ge=0)
    is_staff: bool = False
    # Detection confidence is preserved verbatim. We deliberately do NOT drop or
    # clamp low-confidence events (Part A: "do not suppress low-conf events").
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def _force_utc(cls, v: datetime) -> datetime:
        """Normalise every timestamp to timezone-aware UTC. Naive input is assumed
        UTC (the pipeline emits UTC); offset input is converted."""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    @field_validator("event_id")
    @classmethod
    def _event_id_is_uuid(cls, v: str) -> str:
        """The brief specifies event_id is a uuid-v4. Enforce a parseable UUID at the
        ingest boundary so a malformed id can't silently collide with a real one."""
        try:
            _uuid.UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("event_id must be a valid UUID string")
        return v

    @model_validator(mode="after")
    def _zone_id_rule(self):
        """zone_id must be null for threshold crossings (ENTRY/EXIT/REENTRY) and
        present for zone-scoped events. Enforcing both directions keeps the billing-
        presence logic in analytics from ever being fed a mislabelled event."""
        et = self.event_type if isinstance(self.event_type, str) else self.event_type.value
        if et in _THRESHOLD_TYPES and self.zone_id is not None:
            raise ValueError(f"zone_id must be null for {et} events")
        if et in ("ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL", "BILLING_QUEUE_JOIN",
                  "BILLING_QUEUE_ABANDON") and not self.zone_id:
            raise ValueError(f"zone_id is required for {et} events")
        return self


# ---- ingest request / response shapes -------------------------------------


class IngestRequest(BaseModel):
    events: list[dict] = Field(..., description="raw events; validated per-item for partial success")


class IngestItemError(BaseModel):
    index: int
    event_id: Optional[str] = None
    error: str


class IngestResponse(BaseModel):
    received: int
    accepted: int        # newly persisted
    duplicates: int      # already present -> idempotent no-op
    rejected: int
    errors: list[IngestItemError] = Field(default_factory=list)
