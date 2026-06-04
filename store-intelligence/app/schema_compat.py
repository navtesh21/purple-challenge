"""Boundary normalizer: the messy real-world event schema -> our clean canonical record.

The dataset's `sample_events.jsonl` is the *expected output schema*, and it is three
different shapes from three subsystems, with inconsistent key names:

  entry/exit       : event_type, id_token,  store_code, camera_id, event_timestamp, is_staff,
                     gender_pred, age_pred, age_bucket, is_face_hidden, group_id, group_size
  zone_entered/    : event_type, track_id,  store_id,   camera_id, zone_id, zone_name, zone_type,
   zone_exited       is_revenue_zone, event_time, zone_hotspot_x/y, gender, age, age_bucket
  queue_completed/ : queue_event_id, event_type, track_id, store_id, camera_id, zone_id, ...,
   queue_abandoned   queue_join_ts, queue_served_ts, queue_exit_ts, wait_seconds,
                     queue_position_at_join, abandoned, ...

Our pipeline emits this same schema PLUS a single `visitor_id` on every event (the stable
Re-ID token) so a person can be stitched across families. `normalize_event` collapses any of
these shapes — and our own pipeline output, and the older canonical shape — into the one dict
`app.models.Event` validates and `app.db.EventRow` stores. The rich extra fields (demographics,
queue timings, hotspots) are preserved under `metadata` rather than dropped.

Person key priority: `visitor_id` (our addition) > `id_token` (entry) > `track_id` (zone/queue).
That is what makes "keep the sample schema but link everything with a visitor_id" work, while
still ingesting the provided sample (which has no visitor_id — its families just don't stitch).
"""
from __future__ import annotations

import re
import uuid

# Fixed namespace so a synthesized event_id is DETERMINISTIC: re-ingesting the same event
# yields the same id, preserving idempotency even for events that carry no id of their own.
_NS = uuid.UUID("7f3a1c2e-0b44-4e8a-9c1d-5e6f70819203")

# every spelling we accept -> our canonical UPPER event_type
_TYPE_MAP = {
    "entry": "ENTRY", "exit": "EXIT", "reentry": "REENTRY", "re_entry": "REENTRY",
    "zone_entered": "ZONE_ENTER", "zone_enter": "ZONE_ENTER",
    "zone_exited": "ZONE_EXIT", "zone_exit": "ZONE_EXIT", "zone_dwell": "ZONE_DWELL",
    "queue_completed": "BILLING_QUEUE_JOIN", "queue_join": "BILLING_QUEUE_JOIN",
    "queue_joined": "BILLING_QUEUE_JOIN", "queue_abandoned": "BILLING_QUEUE_ABANDON",
    # already-canonical (our own pipeline / older shape) pass straight through
    "ENTRY": "ENTRY", "EXIT": "EXIT", "REENTRY": "REENTRY",
    "ZONE_ENTER": "ZONE_ENTER", "ZONE_EXIT": "ZONE_EXIT", "ZONE_DWELL": "ZONE_DWELL",
    "BILLING_QUEUE_JOIN": "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON": "BILLING_QUEUE_ABANDON",
}
# the sample uses store_code "store_1076" and store_id "ST1076" for the SAME store;
# unify ONLY that exact pattern (store_ + digits). Anything else (ST1008, STORE_BLR_002)
# is left untouched so we never mangle a store id that merely looks similar.
_STORE_CODE_RE = re.compile(r"^store_(\d+)$")


def _canon_store(raw: dict) -> str:
    s = str(raw.get("store_id") or raw.get("store_code") or raw.get("store") or "").strip()
    m = _STORE_CODE_RE.match(s)
    return "ST" + m.group(1) if m else s


def _pick_ts(raw: dict):
    for k in ("event_timestamp", "event_time", "timestamp", "queue_join_ts"):
        if raw.get(k):
            return raw[k]
    return None


def normalize_event(raw: dict) -> dict:
    """Map any accepted wire shape to the canonical Event dict. Raises ValueError on
    anything we can't make sense of (the caller records it as a rejected item)."""
    if not isinstance(raw, dict):
        raise ValueError("event must be a JSON object")

    src_type = raw.get("event_type") or raw.get("type") or ""
    etype = _TYPE_MAP.get(src_type) or _TYPE_MAP.get(str(src_type).lower())
    if etype is None:
        raise ValueError(f"unknown event_type {src_type!r}")

    store = _canon_store(raw)
    if not store:
        raise ValueError("missing store_id/store_code")
    camera = str(raw.get("camera_id") or raw.get("camera") or "UNKNOWN")

    vid = raw.get("visitor_id") or raw.get("id_token") or raw.get("track_id")
    if vid is None or str(vid) == "":
        raise ValueError("missing visitor_id/id_token/track_id")
    vid = str(vid)

    ts = _pick_ts(raw)
    if ts is None:
        raise ValueError("missing timestamp")

    # Pass zone_id and confidence through unchanged: the canonical Event model enforces the
    # rules (zone null for threshold types, present for zone/queue types; confidence in [0,1]).
    # The normalizer only ROUTES fields; it never silently "fixes" invalid data.
    zone = raw.get("zone_id")
    conf = raw.get("confidence")
    conf = 1.0 if conf is None else float(conf)

    queue_depth = raw.get("queue_position_at_join")
    if queue_depth is None:
        queue_depth = (raw.get("metadata") or {}).get("queue_depth")

    # event_id resolution:
    # * an explicit `event_id` (our own contract field) is passed through verbatim so the
    #   Event model can reject it if it isn't a real UUID — strict on our own field.
    # * the external `queue_event_id` is used only if it's a valid UUID, else we synthesize.
    # * no id at all (entry/zone families) -> synthesize a DETERMINISTIC id from the event
    #   content, preserving idempotency on re-ingest.
    eid = raw.get("event_id")
    if eid is None:
        q = raw.get("queue_event_id")
        if q:
            try:
                uuid.UUID(str(q))
                eid = str(q)
            except (ValueError, AttributeError, TypeError):
                eid = None
        if not eid:
            eid = str(uuid.uuid5(_NS, f"{store}|{vid}|{etype}|{ts}|{zone}|{camera}"))

    md_in = raw.get("metadata") or {}
    meta = {
        "queue_depth": queue_depth,
        "sku_zone": raw.get("sku_zone") or md_in.get("sku_zone"),
        "session_seq": md_in.get("session_seq"),
        # preserved rich fields (kept, never dropped — useful and harmless)
        "gender": raw.get("gender_pred") or raw.get("gender") or md_in.get("gender"),
        "age": raw.get("age_pred") or raw.get("age") or md_in.get("age"),
        "age_bucket": raw.get("age_bucket") or md_in.get("age_bucket"),
        "group_id": raw.get("group_id"),
        "group_size": raw.get("group_size"),
        "is_face_hidden": raw.get("is_face_hidden"),
        "zone_name": raw.get("zone_name"),
        "zone_type": raw.get("zone_type"),
        "is_revenue_zone": raw.get("is_revenue_zone"),
        "wait_seconds": raw.get("wait_seconds"),
        "queue_served_ts": raw.get("queue_served_ts"),
        "queue_exit_ts": raw.get("queue_exit_ts"),
        "abandoned": raw.get("abandoned"),
        "native_id": raw.get("id_token") or raw.get("track_id"),
        "source_event_type": src_type,
    }
    # keep the three schema-required metadata keys always; drop other Nones to stay tidy
    meta = {k: v for k, v in meta.items()
            if v is not None or k in ("queue_depth", "sku_zone", "session_seq")}

    return {
        "event_id": eid,
        "store_id": store,
        "camera_id": camera,
        "visitor_id": vid,
        "event_type": etype,
        "timestamp": ts,
        "zone_id": zone,
        "dwell_ms": int(raw.get("dwell_ms") or 0),
        "is_staff": bool(raw.get("is_staff", False)),
        "confidence": conf,
        "metadata": meta,
    }
