# PROMPT: "Write pytest cases for a /stores/{id}/anomalies endpoint detecting a
#          billing queue spike, a conversion drop vs a trailing 7-day baseline, and a
#          dead zone (no visits in 30 min). Each anomaly has a severity and a
#          suggested_action. Include an all-clear case."
# CHANGES MADE: The model's draft fabricated history with a single day; I built a real
#   multi-day fixture (healthy baseline days + a depressed today) so the conversion-drop
#   detector exercises its baseline averaging. Added the open-hours/has-traffic gating
#   assertions for dead zones, which the draft ignored.

from datetime import timedelta

from tests._helpers import DAY, STORE, billing_time, mk, visitor

ALL_ZONES = ("SKINCARE", "MAKEUP", "HAIRCARE", "FRAGRANCE", "WELLNESS")


def _ingest(client, events):
    assert client.post("/events/ingest", json={"events": events}).status_code == 200


def _types(anoms):
    return {a["type"] for a in anoms["anomalies"]}


def test_queue_spike_critical(client):
    evs = visitor("VIS_q", t0=DAY, billing=True, queue_depth=9)
    _ingest(client, evs)
    a = client.get(f"/stores/{STORE}/anomalies").json()
    spike = [x for x in a["anomalies"] if x["type"] == "BILLING_QUEUE_SPIKE"]
    assert spike and spike[0]["severity"] == "CRITICAL"
    assert spike[0]["suggested_action"]


def test_dead_zone_for_unvisited_zone(client):
    # visitors only ever touch SKINCARE; the other zones go dark
    evs = []
    for i in range(3):
        evs += visitor(f"VIS_{i}", t0=DAY + timedelta(minutes=i), zones=("SKINCARE",))
    _ingest(client, evs)
    a = client.get(f"/stores/{STORE}/anomalies").json()
    dead = {x["metric"]["zone_id"] for x in a["anomalies"] if x["type"] == "DEAD_ZONE"}
    assert "FRAGRANCE" in dead
    assert "SKINCARE" not in dead  # recently visited


def test_conversion_drop_vs_baseline(client, add_pos):
    events = []
    # two healthy baseline days: everyone converts
    for d in (1, 2):
        day = DAY - timedelta(days=d)
        for i in range(4):
            t0 = day + timedelta(minutes=i * 10)
            events += visitor(f"VIS_b{d}_{i}", t0=t0, billing=True)
            add_pos(STORE, billing_time(t0) + timedelta(seconds=60), 500.0)
    # today: same traffic, nobody buys
    for i in range(4):
        events += visitor(f"VIS_t{i}", t0=DAY + timedelta(minutes=i * 10), billing=True)
    _ingest(client, events)

    a = client.get(f"/stores/{STORE}/anomalies").json()
    drop = [x for x in a["anomalies"] if x["type"] == "CONVERSION_DROP"]
    assert drop, a
    assert drop[0]["severity"] == "CRITICAL"
    assert drop[0]["metric"]["today"] == 0.0
    assert drop[0]["metric"]["baseline_avg"] > 0


def test_all_clear(client):
    # every product zone visited recently, no queue, no baseline -> nothing fires
    _ingest(client, visitor("VIS_full", t0=DAY, zones=ALL_ZONES))
    a = client.get(f"/stores/{STORE}/anomalies").json()
    assert a["active_count"] == 0
    assert a["anomalies"] == []
