# PROMPT: "Write pytest cases for a /stores/{id}/funnel endpoint with stages
#          Entry -> Zone Visit -> Billing Queue -> Purchase, counted by session
#          (visitor_id). Re-entries must not double-count a visitor."
# CHANGES MADE: Added the explicit re-entry test (a visitor with ENTRY+REENTRY counts
#   once at every stage) since that is the scored requirement; asserted drop-off math
#   between stages rather than only the raw counts the model first produced.

from datetime import timedelta

from tests._helpers import DAY, STORE, billing_time, visitor


def _ingest(client, *event_lists):
    flat = [e for lst in event_lists for e in lst]
    assert client.post("/events/ingest", json={"events": flat}).status_code == 200


def _stage(funnel, name):
    return next(s for s in funnel["stages"] if s["stage"] == name)


def test_funnel_counts_and_dropoff(client, add_pos):
    entry_only = [visitor(f"VIS_e{i}", t0=DAY + timedelta(minutes=i), zones=()) for i in range(2)]
    zone_only = [visitor(f"VIS_z{i}", t0=DAY + timedelta(minutes=10 + i), zones=("SKINCARE",)) for i in range(3)]
    billers = [visitor(f"VIS_b{i}", t0=DAY + timedelta(minutes=20 + i * 5), billing=True) for i in range(4)]
    _ingest(client, *entry_only, *zone_only, *billers)
    # convert 2 of the 4 billers
    add_pos(STORE, billing_time(DAY + timedelta(minutes=20)) + timedelta(seconds=60), 100.0)
    add_pos(STORE, billing_time(DAY + timedelta(minutes=25)) + timedelta(seconds=60), 100.0)

    f = client.get(f"/stores/{STORE}/funnel").json()
    assert f["sessions"] == 9
    assert _stage(f, "ENTRY")["count"] == 9
    assert _stage(f, "ZONE_VISIT")["count"] == 7   # 3 zone-only + 4 billers
    assert _stage(f, "BILLING_QUEUE")["count"] == 4
    assert _stage(f, "PURCHASE")["count"] == 2
    # drop-off from billing(4) to purchase(2) = 50%
    assert _stage(f, "PURCHASE")["drop_off_from_previous"] == 0.5


def test_reentry_not_double_counted(client, add_pos):
    _ingest(client, visitor("VIS_re", t0=DAY, zones=("SKINCARE",), billing=True, reentry=True))
    add_pos(STORE, billing_time(DAY) + timedelta(seconds=60), 700.0)
    f = client.get(f"/stores/{STORE}/funnel").json()
    assert f["sessions"] == 1
    assert _stage(f, "ENTRY")["count"] == 1   # ENTRY + REENTRY => one session
    assert _stage(f, "BILLING_QUEUE")["count"] == 1
    assert _stage(f, "PURCHASE")["count"] == 1


def test_empty_store_funnel(client):
    f = client.get("/stores/STORE_MUM_001/funnel").json()
    assert f["sessions"] == 0
    assert all(s["count"] == 0 for s in f["stages"])
    assert f["overall_conversion_rate"] == 0.0
