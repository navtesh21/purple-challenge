# PROMPT: "Write pytest cases for a normalizer that maps the dataset's three real event
#          shapes (entry/exit with id_token+store_code+event_timestamp; zone_entered/exited
#          with track_id+store_id+event_time; queue_completed/abandoned with queue_*_ts) into
#          one canonical event, preferring an added `visitor_id`, unifying store_code 'store_NNNN'
#          with store_id 'STNNNN', and synthesizing a deterministic event_id when none is given.
#          Also verify the pipeline's wire builders produce ingestable events end-to-end."
# CHANGES MADE: Embedded representative sample events inline (no dependency on the external
#   sample file); added the determinism check on synthesized event_id (idempotency) and the
#   store-id-not-mangled case (STORE_BLR_002 must stay intact), which the draft missed.

from datetime import datetime, timezone

from app.models import Event
from app.schema_compat import normalize_event

ENTRY = {"event_type": "entry", "id_token": "ID_60001", "store_code": "store_1076",
         "camera_id": "cam1", "event_timestamp": "2026-03-08T18:10:05.120000", "is_staff": False,
         "gender_pred": "F", "age_pred": 28, "age_bucket": "25-34", "group_id": "G_10", "group_size": 2}
ZONE = {"event_type": "zone_entered", "track_id": 101, "store_id": "ST1076", "camera_id": "CAM2",
        "zone_id": "Z01", "zone_name": "Left Shelf", "zone_type": "SHELF", "is_revenue_zone": "Yes",
        "event_time": "2026-03-08T18:10:45.280000", "zone_hotspot_x": 412.6, "zone_hotspot_y": 238.4}
QUEUE_OK = {"queue_event_id": "q-1", "event_type": "queue_completed", "track_id": 102, "store_id": "ST1076",
            "camera_id": "CAM6", "zone_id": "Z_BILLING_01", "zone_name": "Billing", "zone_type": "BILLING",
            "queue_join_ts": "2026-03-08T18:13:05", "queue_served_ts": "2026-03-08T18:13:13",
            "queue_exit_ts": "2026-03-08T18:15:31", "wait_seconds": 8, "queue_position_at_join": 2, "abandoned": False}
QUEUE_ABANDON = {**QUEUE_OK, "queue_event_id": "q-2", "event_type": "queue_abandoned",
                 "track_id": 103, "abandoned": True, "queue_served_ts": None}


def _norm(raw):
    return Event.model_validate(normalize_event(raw))   # normalize -> validate canonical


def test_entry_family():
    e = _norm(ENTRY)
    assert e.event_type == "ENTRY"
    assert e.visitor_id == "ID_60001"          # from id_token
    assert e.store_id == "ST1076"              # store_code 'store_1076' unified
    assert e.zone_id is None                   # threshold events carry no zone
    assert e.metadata.model_dump()["gender"] == "F"


def test_zone_family():
    e = _norm(ZONE)
    assert e.event_type == "ZONE_ENTER"
    assert e.visitor_id == "101"               # from track_id
    assert e.zone_id == "Z01"
    assert e.metadata.model_dump()["zone_name"] == "Left Shelf"


def test_queue_families_and_served_flag():
    done = _norm(QUEUE_OK)
    assert done.event_type == "BILLING_QUEUE_JOIN"
    assert done.metadata.model_dump()["abandoned"] is False    # -> "served" in analytics
    assert done.metadata.queue_depth == 2                       # from queue_position_at_join
    ab = _norm(QUEUE_ABANDON)
    assert ab.event_type == "BILLING_QUEUE_ABANDON"
    assert ab.metadata.model_dump()["abandoned"] is True


def test_visitor_id_is_preferred_link():
    raw = {**ZONE, "visitor_id": "VIS_LINK_7"}   # our added cross-family key
    assert _norm(raw).visitor_id == "VIS_LINK_7"


def test_store_code_unify_but_dont_mangle():
    assert normalize_event(ENTRY)["store_id"] == "ST1076"
    keep = {**ENTRY, "store_code": None, "store_id": "STORE_BLR_002"}
    assert normalize_event(keep)["store_id"] == "STORE_BLR_002"   # not 'STBLR_002'


def test_synthesized_event_id_is_deterministic():
    # same content -> same id (idempotent re-ingest); entry/zone events carry no id of their own
    assert normalize_event(ENTRY)["event_id"] == normalize_event(ENTRY)["event_id"]
    assert normalize_event(ENTRY)["event_id"] != normalize_event(ZONE)["event_id"]


def test_unknown_type_rejected():
    import pytest
    with pytest.raises(ValueError):
        normalize_event({"event_type": "teleport", "id_token": "x", "store_id": "ST1",
                         "event_timestamp": "2026-01-01T00:00:00"})


def test_ingest_three_families_end_to_end(client):
    r = client.post("/events/ingest", json={"events": [ENTRY, ZONE, QUEUE_OK, QUEUE_ABANDON]})
    assert r.status_code == 200
    assert r.json()["accepted"] == 4 and r.json()["rejected"] == 0


def test_pipeline_wire_builders_ingest(client):
    from pipeline.emit import make_entry, make_queue, make_zone
    t = datetime(2026, 4, 10, 14, 40, tzinfo=timezone.utc)
    evs = [
        make_entry(store_id="ST1008", camera_id="CAM_ENTRY_01", visitor_id="VIS_1",
                   event_type="entry", timestamp=t),
        make_zone(store_id="ST1008", camera_id="CAM_SKIN_01", visitor_id="VIS_1",
                  event_type="zone_entered", timestamp=t, zone_id="SKIN", hotspot=(1.0, 2.0)),
        make_queue(store_id="ST1008", camera_id="CAM_BILLING_01", visitor_id="VIS_1",
                   abandoned=False, zone_id="BILLING", join_ts=t, served_ts=t, exit_ts=t, queue_position=0),
    ]
    r = client.post("/events/ingest", json={"events": evs}).json()
    assert r["accepted"] == 3 and r["rejected"] == 0
