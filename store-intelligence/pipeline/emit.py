"""Event emission + transport.

The pipeline emits events in the dataset's real wire schema (the three families seen in
`sample_events.jsonl`): entry/exit, zone_entered/zone_exited, queue_completed/queue_abandoned.
We keep every native field name from the sample and ADD one `visitor_id` to each event — our
stable Re-ID token — so the same person can be stitched across the three families (entry uses
`id_token`, zone/queue use `track_id`; all three also carry `visitor_id` with the same value).

Every builder validates its output by running it through the API's normalizer + `Event` model,
so a malformed event fails here in the pipeline, never on ingest. Two sinks: a JSONL file
(batch) or batched HTTP POST (live).
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.models import Event  # noqa: E402  (path bootstrap first)
from app.schema_compat import normalize_event  # noqa: E402


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _validate(wire: dict) -> dict:
    """Assert the wire event will ingest (normalize -> canonical -> schema). Returns it."""
    Event.model_validate(normalize_event(wire))
    return wire


def make_entry(*, store_id, camera_id, visitor_id, event_type, timestamp,
               is_staff=False, confidence=0.9, group_id=None, group_size=None,
               gender=None, age=None, age_bucket=None) -> dict:
    """entry / exit / reentry — keyed by id_token, plus our visitor_id."""
    return _validate({
        "event_type": event_type,            # "entry" | "exit" | "reentry"
        "id_token": visitor_id,
        "visitor_id": visitor_id,            # our added cross-family link
        "store_code": store_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "event_timestamp": _iso(timestamp),
        "is_staff": is_staff,
        "gender_pred": gender, "age_pred": age, "age_bucket": age_bucket,
        "is_face_hidden": True,              # footage is face-blurred
        "group_id": group_id, "group_size": group_size,
        "confidence": round(float(confidence), 3),
    })


def make_zone(*, store_id, camera_id, visitor_id, event_type, timestamp, zone_id,
              zone_name=None, zone_type="SHELF", is_revenue_zone="Yes",
              hotspot=(None, None), is_staff=False, confidence=0.9,
              gender=None, age=None, age_bucket=None) -> dict:
    """zone_entered / zone_exited — keyed by track_id, plus our visitor_id."""
    return _validate({
        "event_type": event_type,            # "zone_entered" | "zone_exited"
        "track_id": visitor_id,
        "visitor_id": visitor_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "zone_id": zone_id,
        "zone_name": zone_name or zone_id,
        "zone_type": zone_type,
        "is_revenue_zone": is_revenue_zone,
        "event_time": _iso(timestamp),
        "zone_hotspot_x": hotspot[0], "zone_hotspot_y": hotspot[1],
        "is_staff": is_staff,
        "gender": gender, "age": age, "age_bucket": age_bucket,
        "confidence": round(float(confidence), 3),
    })


def make_queue(*, store_id, camera_id, visitor_id, abandoned, zone_id,
               join_ts, served_ts, exit_ts, queue_position, zone_name="Billing Counter Queue",
               is_staff=False, confidence=0.9) -> dict:
    """queue_completed / queue_abandoned — one event per billing episode, keyed by track_id."""
    wait = int((exit_ts - join_ts).total_seconds())
    return _validate({
        "queue_event_id": str(uuid.uuid4()),
        "event_type": "queue_abandoned" if abandoned else "queue_completed",
        "track_id": visitor_id,
        "visitor_id": visitor_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "zone_id": zone_id,
        "zone_name": zone_name,
        "zone_type": "BILLING",
        "is_revenue_zone": "Yes",
        "queue_join_ts": _iso(join_ts),
        "queue_served_ts": _iso(served_ts) if served_ts else None,
        "queue_exit_ts": _iso(exit_ts),
        "wait_seconds": wait,
        "queue_position_at_join": queue_position,
        "abandoned": bool(abandoned),
        "is_staff": is_staff,
        "confidence": round(float(confidence), 3),
    })


class JsonlSink:
    """Append events to a JSONL file (batch mode)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8")
        self.count = 0

    def emit(self, event: dict) -> None:
        self._fh.write(json.dumps(event) + "\n")
        self.count += 1

    def emit_many(self, events: Iterable[dict]) -> None:
        for e in events:
            self.emit(e)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class HttpSink:
    """POST events to the API in batches (live / simulated-real-time mode)."""

    def __init__(self, base_url: str, batch_size: int = 200, timeout: float = 10.0):
        import requests  # local import: only needed for HTTP transport

        self._requests = requests
        self.url = base_url.rstrip("/") + "/events/ingest"
        self.batch_size = batch_size
        self.timeout = timeout
        self._buf: list[dict] = []
        self.sent = 0

    def emit(self, event: dict) -> None:
        self._buf.append(event)
        if len(self._buf) >= self.batch_size:
            self.flush()

    def emit_many(self, events: Iterable[dict]) -> None:
        for e in events:
            self.emit(e)

    def flush(self) -> dict | None:
        if not self._buf:
            return None
        resp = self._requests.post(self.url, json={"events": self._buf}, timeout=self.timeout)
        resp.raise_for_status()
        self.sent += len(self._buf)
        self._buf.clear()
        return resp.json()

    def close(self) -> None:
        self.flush()
