# PROMPT: "Write pytest cases for a FastAPI POST /events/ingest endpoint that must be
#          idempotent by event_id, support partial success on malformed events, dedup
#          within a batch, and reject batches over 500. Use a TestClient."
# CHANGES MADE: Tightened assertions to the exact IngestResponse 4-way counters
#   (received/accepted/duplicates/rejected); added the in-batch duplicate case and
#   the 400-on-oversize-batch case, which the model's first draft omitted; reused the
#   shared mk() builder instead of inline dicts.

from datetime import timedelta

from tests._helpers import DAY, mk, visitor


def test_ingest_accepts_and_counts(client):
    evs = visitor("VIS_a1", zones=("SKINCARE", "MAKEUP"))
    r = client.post("/events/ingest", json={"events": evs})
    assert r.status_code == 200
    body = r.json()
    assert body["received"] == len(evs)
    assert body["accepted"] == len(evs)
    assert body["duplicates"] == 0
    assert body["rejected"] == 0


def test_ingest_is_idempotent(client):
    evs = visitor("VIS_idem")
    first = client.post("/events/ingest", json={"events": evs}).json()
    second = client.post("/events/ingest", json={"events": evs}).json()
    assert first["accepted"] == len(evs)
    assert second["accepted"] == 0
    assert second["duplicates"] == len(evs)  # re-sending the same payload changes nothing


def test_partial_success_on_malformed(client):
    good = mk("ENTRY", vid="VIS_ok", camera="CAM_ENTRY_01")
    bad_conf = mk("ZONE_ENTER", vid="VIS_bad", zone="SKINCARE")
    bad_conf["confidence"] = 5.0  # out of [0,1]
    bad_type = mk("ENTRY", vid="VIS_bad2")
    bad_type["event_type"] = "TELEPORT"  # not in the enum
    r = client.post("/events/ingest", json={"events": [good, bad_conf, bad_type]})
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 2
    assert {e["index"] for e in body["errors"]} == {1, 2}


def test_in_batch_duplicates_collapse(client):
    e = mk("ENTRY", vid="VIS_dup", camera="CAM_ENTRY_01")
    r = client.post("/events/ingest", json={"events": [e, e, e]})
    body = r.json()
    assert body["received"] == 3
    assert body["accepted"] == 1
    assert body["duplicates"] == 2


def test_oversize_batch_rejected_400(client):
    evs = [mk("ENTRY", vid=f"V{i}", camera="CAM_ENTRY_01") for i in range(501)]
    r = client.post("/events/ingest", json={"events": evs})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_empty_batch_is_fine(client):
    r = client.post("/events/ingest", json={"events": []})
    assert r.status_code == 200
    assert r.json() == {"received": 0, "accepted": 0, "duplicates": 0, "rejected": 0, "errors": []}


def test_zone_id_null_rule_enforced(client):
    # ENTRY/EXIT/REENTRY must have null zone_id; zone events must have a zone_id
    entry_with_zone = mk("ENTRY", vid="VIS_z", camera="CAM_ENTRY_01", zone="SKINCARE")
    zone_without_id = mk("ZONE_ENTER", vid="VIS_z2", zone=None)
    r = client.post("/events/ingest", json={"events": [entry_with_zone, zone_without_id]})
    body = r.json()
    assert body["accepted"] == 0
    assert body["rejected"] == 2


def test_event_id_must_be_uuid(client):
    bad = mk("ENTRY", vid="VIS_u", camera="CAM_ENTRY_01")
    bad["event_id"] = "not-a-uuid"
    r = client.post("/events/ingest", json={"events": [bad]})
    body = r.json()
    assert body["rejected"] == 1
    assert "event_id" in body["errors"][0]["error"]
