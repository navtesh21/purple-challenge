# PROMPT: "Write pytest cases for a /stores/{id}/metrics endpoint computing unique
#          visitors, conversion rate via POS time-window correlation, dwell per zone
#          and abandonment. Must exclude staff, handle zero-traffic and zero-purchase
#          stores without nulls or 500s."
# CHANGES MADE: Staggered each converting visitor's entry time so billing-presence
#   times are distinct and one POS row maps to exactly one converter (the model's draft
#   put all visitors at the same time, so a single POS marked everyone converted — a
#   real property of time-window correlation I instead made explicit in test_conversion_
#   correlation_is_time_windowed). Added the all-staff and zero-traffic cases.

from datetime import timedelta

from tests._helpers import DAY, STORE, billing_time, visitor


def _ingest(client, *event_lists):
    flat = [e for lst in event_lists for e in lst]
    r = client.post("/events/ingest", json={"events": flat})
    assert r.status_code == 200
    return r.json()


def test_metrics_basic_conversion_and_staff_exclusion(client, add_pos):
    # 3 billing visitors staggered 10 min apart; only the first two buy
    v1 = visitor("VIS_1", t0=DAY, billing=True)
    v2 = visitor("VIS_2", t0=DAY + timedelta(minutes=10), billing=True)
    v3 = visitor("VIS_3", t0=DAY + timedelta(minutes=20), billing=True)
    browse = visitor("VIS_4", t0=DAY + timedelta(minutes=5), zones=("MAKEUP",))
    staff = visitor("VIS_staff", t0=DAY + timedelta(minutes=2), billing=True, is_staff=True)
    _ingest(client, v1, v2, v3, browse, staff)

    add_pos(STORE, billing_time(DAY) + timedelta(seconds=60), 1200.0)
    add_pos(STORE, billing_time(DAY + timedelta(minutes=10)) + timedelta(seconds=60), 800.0)

    m = client.get(f"/stores/{STORE}/metrics").json()
    assert m["unique_visitors"] == 4              # staff excluded
    assert m["converted_visitors"] == 2
    assert m["conversion_rate"] == 0.5
    assert m["purchases"] == 2
    assert m["billing_visitors"] == 3
    assert m["is_zero_traffic"] is False
    assert "SKINCARE" in m["avg_dwell_seconds_per_zone"]


def test_zero_traffic_store_is_safe(client):
    m = client.get("/stores/STORE_HYD_001/metrics").json()
    assert m["unique_visitors"] == 0
    assert m["conversion_rate"] == 0.0
    assert m["is_zero_traffic"] is True
    assert m["avg_dwell_seconds_per_zone"] == {}


def test_zero_purchase_store(client):
    # visitors browse and queue but no POS rows exist -> conversion 0, no crash
    _ingest(client, visitor("VIS_a", billing=True), visitor("VIS_b", billing=True))
    m = client.get(f"/stores/{STORE}/metrics").json()
    assert m["unique_visitors"] == 2
    assert m["converted_visitors"] == 0
    assert m["conversion_rate"] == 0.0


def test_all_staff_clip_excluded(client, add_pos):
    _ingest(client, visitor("VIS_s1", billing=True, is_staff=True),
            visitor("VIS_s2", billing=True, is_staff=True))
    add_pos(STORE, billing_time(DAY) + timedelta(seconds=60), 999.0)
    m = client.get(f"/stores/{STORE}/metrics").json()
    assert m["unique_visitors"] == 0
    assert m["is_zero_traffic"] is True
    assert m["conversion_rate"] == 0.0


def test_abandonment_rate(client):
    _ingest(client,
            visitor("VIS_ab", t0=DAY, billing=True, abandon=True, queue_depth=7),
            visitor("VIS_ok", t0=DAY + timedelta(minutes=10), billing=True))
    m = client.get(f"/stores/{STORE}/metrics").json()
    assert m["billing_visitors"] == 2
    assert m["abandonment_rate"] == 0.5  # one of two billing visitors abandoned


def test_conversion_correlation_is_time_windowed(client, add_pos):
    # a POS more than the 300s window after billing presence must NOT convert
    _ingest(client, visitor("VIS_late", t0=DAY, billing=True))
    add_pos(STORE, billing_time(DAY) + timedelta(seconds=600), 500.0)  # outside window
    m = client.get(f"/stores/{STORE}/metrics").json()
    assert m["converted_visitors"] == 0
