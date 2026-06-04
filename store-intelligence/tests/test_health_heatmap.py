# PROMPT: "Write pytest cases for GET /health (per-store last event timestamp, lag,
#          STALE_FEED past a 10-min threshold, DB status) and GET /stores/{id}/heatmap
#          (normalised 0-100 intensities, data_confidence LOW under 20 sessions)."
# CHANGES MADE: Used a genuinely fresh (now) event for one store and an old event for
#   another so the STALE_FEED boundary is exercised against wall-clock now, matching the
#   endpoint's real semantics rather than a frozen clock the model assumed.

from datetime import datetime, timedelta, timezone

from tests._helpers import DAY, STORE, mk, visitor


def _ingest(client, events):
    assert client.post("/events/ingest", json={"events": events}).status_code == 200


def test_health_flags_stale_and_fresh(client):
    now = datetime.now(timezone.utc)
    fresh = mk("ENTRY", vid="VIS_fresh", store="STORE_BLR_002", camera="CAM_ENTRY_01", t=now)
    stale = mk("ENTRY", vid="VIS_stale", store="STORE_BLR_001", camera="CAM_ENTRY_01", t=DAY)
    _ingest(client, [fresh, stale])

    h = client.get("/health").json()
    assert h["db"] == "up"
    by_id = {s["store_id"]: s for s in h["stores"]}
    assert by_id["STORE_BLR_002"]["status"] == "OK"
    assert by_id["STORE_BLR_001"]["status"] == "STALE_FEED"
    assert "STALE_FEED:STORE_BLR_001" in h["warnings"]
    assert h["status"] == "warning"


def test_health_reports_no_data_stores(client):
    # a layout store with no events at all -> NO_DATA, never a crash
    _ingest(client, [mk("ENTRY", vid="VIS_x", store=STORE, camera="CAM_ENTRY_01")])
    h = client.get("/health").json()
    statuses = {s["store_id"]: s["status"] for s in h["stores"]}
    assert statuses.get("STORE_MUM_001") == "NO_DATA"


def test_heatmap_low_confidence_and_normalisation(client):
    # a handful of visitors -> < 20 sessions -> LOW confidence
    evs = []
    for i in range(3):
        evs += visitor(f"VIS_h{i}", t0=DAY + timedelta(minutes=i), zones=("SKINCARE", "MAKEUP"))
    evs += visitor("VIS_extra", t0=DAY + timedelta(minutes=9), zones=("SKINCARE",))
    _ingest(client, evs)

    h = client.get(f"/stores/{STORE}/heatmap").json()
    assert h["data_confidence"] == "LOW"
    cells = {c["zone_id"]: c for c in h["cells"]}
    # SKINCARE visited by 4, MAKEUP by 3 -> SKINCARE is the busiest -> intensity 100
    assert cells["SKINCARE"]["visits"] == 4
    assert cells["SKINCARE"]["visit_intensity"] == 100.0
    assert cells["MAKEUP"]["visits"] == 3
    # an unvisited product zone still appears in the grid at zero
    assert cells["FRAGRANCE"]["visits"] == 0


def test_heatmap_only_product_zones(client):
    _ingest(client, visitor("VIS_b", t0=DAY, zones=("SKINCARE",), billing=True))
    h = client.get(f"/stores/{STORE}/heatmap").json()
    zones = {c["zone_id"] for c in h["cells"]}
    assert "BILLING" not in zones      # billing is not an attention zone
    assert "ENTRANCE" not in zones
