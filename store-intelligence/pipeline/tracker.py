"""Tracking, direction, Re-ID, staff and zone logic — the reasoning layer that
turns raw per-frame person detections into behavioural state.

The detector (YOLO) gives us boxes per frame. ByteTrack (run inside ultralytics)
gives each box a short-lived `track_id` within one camera clip. That is not enough:

* track_ids reset on occlusion and never span cameras, so they would inflate the
  visitor count badly. `ReIDGallery` maps unstable track_ids to a STABLE
  `visitor_id` using an appearance signature + spatial/temporal gating, which is
  also what catches **re-entry** (same person returns) and **camera overlap**
  (same person on two cameras).
* `LineCrossing` decides entry vs exit from the *direction* a centroid crosses the
  door line, not mere presence.
* `StaffClassifier` flags staff (uniform colour signature) so they are excluded
  from customer metrics — with a documented hook for a VLM second opinion.
* `ZoneMapper` resolves a point to a named zone via polygons declared per camera in
  store_layout.json.

These are deliberately classical / explainable methods (histogram Re-ID, line
crossing, colour-based staff) rather than a heavyweight Re-ID network: on 1080p/15fps
blurred CCTV they are fast, debuggable, and good enough — and CHOICES.md records
exactly when we would switch to OSNet embeddings instead.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np


# --- appearance signature ---------------------------------------------------


def appearance_signature(crop: np.ndarray) -> np.ndarray:
    """A small, illumination-tolerant colour signature for a person crop.

    HSV hue+saturation histogram (value channel dropped to blunt lighting changes,
    which the brief warns vary across clips). L1-normalised so it behaves like a
    distribution; compared with cosine similarity in the gallery.
    """
    import cv2

    if crop.size == 0:
        return np.zeros(32, dtype=np.float32)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 2], [0, 180, 0, 256])
    hist = hist.flatten().astype(np.float32)
    s = hist.sum()
    return hist / s if s > 0 else hist


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# --- Re-ID gallery -----------------------------------------------------------


@dataclass
class GalleryEntry:
    visitor_id: str
    signature: np.ndarray
    last_seen: datetime
    exited: bool = False


class ReIDGallery:
    """Maps unstable per-camera track_ids to stable visitor_ids.

    Matching rule: a new/lost track is matched to a gallery entry if appearance
    cosine similarity exceeds `sim_threshold` AND it was last seen within
    `ttl_seconds`. A match that lands on an already-EXITed entry is reported as a
    re-entry. No match -> a fresh visitor_id.
    """

    def __init__(self, sim_threshold: float = 0.72, ttl_seconds: int = 900):
        self.sim_threshold = sim_threshold
        self.ttl = timedelta(seconds=ttl_seconds)
        self._by_track: dict[tuple[str, int], str] = {}  # (camera, track_id) -> visitor_id
        self._gallery: dict[str, GalleryEntry] = {}
        self._counter = 0

    def _new_visitor(self, sig: np.ndarray, ts: datetime) -> str:
        self._counter += 1
        vid = f"VIS_{self._counter:06x}"
        self._gallery[vid] = GalleryEntry(visitor_id=vid, signature=sig, last_seen=ts)
        return vid

    def assign(self, camera: str, track_id: int, sig: np.ndarray, ts: datetime) -> tuple[str, bool]:
        """Return (visitor_id, is_reentry) for a track observation."""
        key = (camera, track_id)
        if key in self._by_track:
            vid = self._by_track[key]
            e = self._gallery[vid]
            e.last_seen = ts
            # rolling average keeps the signature current under lighting drift
            e.signature = 0.8 * e.signature + 0.2 * sig
            return vid, False

        # try to match an existing (possibly exited) visitor
        best_vid, best_sim = None, 0.0
        for vid, e in self._gallery.items():
            if ts - e.last_seen > self.ttl:
                continue
            sim = _cosine(sig, e.signature)
            if sim > best_sim:
                best_vid, best_sim = vid, sim

        if best_vid is not None and best_sim >= self.sim_threshold:
            e = self._gallery[best_vid]
            is_reentry = e.exited
            e.exited = False
            e.last_seen = ts
            self._by_track[key] = best_vid
            return best_vid, is_reentry

        vid = self._new_visitor(sig, ts)
        self._by_track[key] = vid
        return vid, False

    def mark_exit(self, visitor_id: str) -> None:
        if visitor_id in self._gallery:
            self._gallery[visitor_id].exited = True


# --- direction (entry vs exit) ----------------------------------------------


@dataclass
class LineCrossing:
    """Signed-side test against the door line p1->p2.

    The sign of the 2D cross product tells which side of the line a point is on. A
    change of sign between consecutive centroids = a crossing; the direction of the
    change distinguishes ENTRY (inbound) from EXIT (outbound).
    """

    p1: tuple[float, float]
    p2: tuple[float, float]
    inbound_positive: bool = True

    def side(self, pt: tuple[float, float]) -> float:
        (x1, y1), (x2, y2) = self.p1, self.p2
        x, y = pt
        return (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)

    def crossing(self, prev: tuple[float, float], cur: tuple[float, float]) -> str | None:
        a, b = self.side(prev), self.side(cur)
        if a == 0 or b == 0 or (a > 0) == (b > 0):
            return None
        going_positive = b > 0
        inbound = going_positive == self.inbound_positive
        return "ENTRY" if inbound else "EXIT"


# --- staff classifier --------------------------------------------------------


def _torso_crop(crop: np.ndarray) -> np.ndarray:
    """Return the central torso strip of a person bounding-box crop.

    Hair (top ~25%) and legs/shoes (bottom ~30%) are excluded because they
    produce systematic false positives for both the red and the dark checks:
    - Hair → dark pixels that inflate `dark_uniform_fraction`
    - Dark jeans → dark pixels on customers
    - Background slipping in at the sides of loose bounding boxes

    Keeping rows 25–70% and columns 20–80% leaves the shirt/uniform region,
    which is where the staff uniform colour actually lives.
    """
    if crop is None or crop.size == 0:
        return crop
    h, w = crop.shape[:2]
    r0, r1 = max(0, int(h * 0.25)), max(1, int(h * 0.70))
    c0, c1 = max(0, int(w * 0.20)), max(1, int(w * 0.80))
    t = crop[r0:r1, c0:c1]
    return t if t.size > 0 else crop


def red_uniform_fraction(crop: np.ndarray) -> float:
    """Fraction of the *torso region* of a person crop that is saturated red.

    Purplle Store 1 (Brigade Road) staff wear red uniforms. Red wraps around
    the OpenCV hue circle (hue 0–10 and 170–180); we require decent saturation
    (S ≥ 90) and brightness (V ≥ 60) so dark maroon shadows or brownish skin
    don't count. The torso crop avoids hair and legs polluting the fraction.
    """
    import cv2

    crop = _torso_crop(crop)
    if crop is None or crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((h <= 10) | (h >= 170)) & (s >= 90) & (v >= 60)
    return float(red.mean())


def dark_uniform_fraction(crop: np.ndarray) -> float:
    """Fraction of the *torso region* of a person crop that is dark/black.

    Purplle Store 2 staff wear black uniforms. We check V < 60 AND S < 60 so
    true blacks and dark greys qualify, while colourful dark clothing (dark blue
    jeans, navy, dark green) is excluded by the low-saturation requirement.

    **Why the torso crop is critical here:** black is far less distinctive than
    red — customer hair, dark jeans, shadows, and dark handbags all produce dark
    pixels. Restricting to the torso region (shirt area) eliminates hair at the
    top and trouser legs at the bottom, which are the main sources of false
    positives. Without this crop the check would flag most customers wearing
    dark clothing.
    """
    import cv2

    crop = _torso_crop(crop)
    if crop is None or crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    dark = (v < 60) & (s < 60)
    return float(dark.mean())


# Map colour name (from store_layout.json `staff_uniform` list) to the fraction function.
_UNIFORM_CHECKERS = {
    "red": red_uniform_fraction,
    "dark": dark_uniform_fraction,
    "black": dark_uniform_fraction,   # alias
}


class StaffClassifier:
    """Flags staff by uniform colour.

    `uniform_colors` is a list of colour names (e.g. ``["red"]``, ``["dark"]``,
    ``["red", "dark"]``) read from ``store_layout.json`` → ``staff_uniform``. For each
    configured colour the corresponding HSV pixel-fraction function is called on the
    **torso region** of the crop; if *any* exceeds `min_fraction` the person is flagged
    as staff.

    Supported colour names: ``"red"`` (saturated red, hue wraps 0-10/170-180),
    ``"dark"`` / ``"black"`` (low-brightness, low-saturation pixels).

    Optionally also matches a learned `uniform_signature` via cosine similarity, and
    accepts a `vlm_hook` for a VLM second opinion on ambiguous crops (see CHOICES.md).
    """

    def __init__(
        self,
        uniform_colors: list[str] | None = None,
        min_fraction: float = 0.20,
        uniform_signature: np.ndarray | None = None,
        signature_threshold: float = 0.8,
        vlm_hook=None,
    ):
        # Default to red if nothing is configured (backwards-compatible with ST1008).
        self.checkers = [
            _UNIFORM_CHECKERS[c.lower()]
            for c in (uniform_colors or ["red"])
            if c.lower() in _UNIFORM_CHECKERS
        ]
        self.min_fraction = min_fraction
        self.uniform = uniform_signature
        self.threshold = signature_threshold
        self.vlm_hook = vlm_hook

    def is_staff(self, crop: np.ndarray) -> bool:
        # Pixel-fraction check for each configured uniform colour.
        for check in self.checkers:
            if check(crop) >= self.min_fraction:
                return True
        # Optional: learned appearance-signature match.
        if self.uniform is not None and _cosine(appearance_signature(crop), self.uniform) >= self.threshold:
            return True
        # Optional: VLM second opinion.
        if self.vlm_hook is not None:
            return bool(self.vlm_hook(crop))
        return False


# --- zone mapping ------------------------------------------------------------


class ZoneMapper:
    """Resolves an image point to a named zone using per-camera polygons from
    store_layout.json (`cameras.<cam>.zones_px: {ZONE: [[x,y],...]}`). When a camera
    declares no polygons, every point maps to the camera's single covered zone."""

    def __init__(self, camera_cfg: dict):
        self.polygons = camera_cfg.get("zones_px", {})
        covers = camera_cfg.get("covers", [])
        self.default_zone = covers[0] if len(covers) == 1 else None

    def zone_for(self, pt: tuple[float, float]) -> str | None:
        if not self.polygons:
            return self.default_zone
        for zone, poly in self.polygons.items():
            if _point_in_poly(pt, poly):
                return zone
        return self.default_zone


def _point_in_poly(pt, poly) -> bool:
    """Ray-casting point-in-polygon."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-9) + x1
            if x < xin:
                inside = not inside
    return inside


# --- dwell tracking ----------------------------------------------------------


@dataclass
class DwellState:
    """Tracks a visitor's current zone and accumulated dwell so we can emit
    ZONE_ENTER / ZONE_DWELL (every 30s) / ZONE_EXIT correctly.

    `pending_zone`/`pending_count` implement zone-change hysteresis: a candidate new
    zone must be observed for several consecutive frames before the switch is committed,
    so a centroid jittering across a zone boundary doesn't emit a storm of
    ZONE_ENTER/ZONE_EXIT events."""

    zone: str | None = None
    entered_at: datetime | None = None
    last_dwell_emit: datetime | None = None
    pending_zone: str | None = None
    pending_count: int = 0
    history: deque = field(default_factory=lambda: deque(maxlen=30))
