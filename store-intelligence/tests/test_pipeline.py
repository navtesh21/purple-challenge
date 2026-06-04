# PROMPT: "Write pytest cases for the detection pipeline's reasoning units without
#          needing video or a GPU: a signed-line entry/exit crossing detector, a
#          histogram Re-ID gallery that reuses a visitor_id on re-entry and across
#          cameras, a point-in-polygon zone mapper, and schema validation in the event
#          emitter."
# CHANGES MADE: Drove the Re-ID gallery with hand-built numpy signatures (orthogonal =
#   different person, identical = same person) so the cosine threshold behaviour is
#   deterministic and doesn't depend on opencv; asserted the re-entry flag flips only
#   after mark_exit, which is the property the REENTRY event depends on.

import numpy as np

from pipeline import tracker as T
from pipeline.emit import make_entry
from datetime import datetime, timedelta, timezone


def test_line_crossing_distinguishes_direction():
    line = T.LineCrossing((5, 0), (5, 10), inbound_positive=True)
    left_to_right = line.crossing((4, 5), (6, 5))
    right_to_left = line.crossing((6, 5), (4, 5))
    assert left_to_right is not None and right_to_left is not None
    assert left_to_right != right_to_left           # opposite directions disagree
    assert {left_to_right, right_to_left} == {"ENTRY", "EXIT"}


def test_line_crossing_no_crossing_when_same_side():
    line = T.LineCrossing((5, 0), (5, 10))
    assert line.crossing((1, 1), (2, 2)) is None     # both on the same side


def test_reid_gallery_reentry_and_cross_camera():
    g = T.ReIDGallery(sim_threshold=0.72, ttl_seconds=900)
    t0 = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    person_a = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    person_b = np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=np.float32)

    vid1, re1 = g.assign("CAM_ENTRY_01", 1, person_a, t0)
    assert re1 is False
    # same track id -> same visitor, still not a re-entry
    vid1b, _ = g.assign("CAM_ENTRY_01", 1, person_a, t0 + timedelta(seconds=1))
    assert vid1b == vid1

    # different person -> new visitor id
    vid2, re2 = g.assign("CAM_ENTRY_01", 2, person_b, t0 + timedelta(seconds=2))
    assert vid2 != vid1 and re2 is False

    # visitor 1 exits, then reappears on a DIFFERENT camera/track -> re-entry, same id
    g.mark_exit(vid1)
    vid_re, is_re = g.assign("CAM_FLOOR_01", 99, person_a, t0 + timedelta(seconds=120))
    assert vid_re == vid1
    assert is_re is True


def test_zone_mapper_polygon_and_default():
    cfg = {"covers": ["A", "B"], "zones_px": {"A": [[0, 0], [10, 0], [10, 10], [0, 10]]}}
    zm = T.ZoneMapper(cfg)
    assert zm.zone_for((5, 5)) == "A"
    assert zm.zone_for((50, 50)) is None              # outside, no single default

    single = T.ZoneMapper({"covers": ["FLOOR"]})       # no polygons, one covered zone
    assert single.zone_for((123, 456)) == "FLOOR"


def test_point_in_poly():
    square = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert T._point_in_poly((5, 5), square) is True
    assert T._point_in_poly((11, 5), square) is False


def test_make_entry_emits_wire_shape():
    ev = make_entry(store_id="ST1008", camera_id="CAM_ENTRY_01", visitor_id="VIS_9",
                    event_type="entry", timestamp=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
                    confidence=0.9)
    # native sample field names PLUS our added visitor_id link
    assert ev["event_type"] == "entry"
    assert ev["id_token"] == "VIS_9" and ev["visitor_id"] == "VIS_9"
    assert ev["is_face_hidden"] is True


def test_make_entry_rejects_bad_confidence():
    import pytest
    with pytest.raises(Exception):  # builder validates via normalize -> Event (confidence le 1)
        make_entry(store_id="ST1008", camera_id="CAM_ENTRY_01", visitor_id="VIS_9",
                   event_type="entry", timestamp=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
                   confidence=2.0)
