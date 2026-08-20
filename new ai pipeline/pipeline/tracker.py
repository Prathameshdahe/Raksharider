"""
pipeline/tracker.py
───────────────────
ByteTrack-style multi-object tracker for the DriveTrust AI pipeline.

Assigns stable integer IDs to person and road-vehicle detections across
frames so the same physical object keeps the same ID throughout a clip,
instead of being re-counted every frame.

Algorithm
---------
ByteTrack uses two IoU-matching passes per frame:

  Pass 1 (high-confidence detections ≥ HIGH_CONF_THRESHOLD):
      Match against active tracks using Hungarian assignment on IoU cost.
      Matched  → update track, keep ID
      Unmatched detections → tentative new tracks
      Unmatched tracks     → move to "lost" pool

  Pass 2 (low-confidence detections < HIGH_CONF_THRESHOLD):
      Match unmatched detections from Pass 1 against lost tracks.
      Matched  → revive lost track with original ID
      Remaining unmatched detections → discard (noise)

  Track lifecycle:
      new      → confirmed (after MIN_HITS consecutive hits)
      confirmed → lost     (after MAX_AGE consecutive misses)
      lost     → removed   (after LOST_TTL frames in lost state)

Usage
-----
    from pipeline.tracker import Tracker
    tracker = Tracker()
    for frame_detections in all_frame_detections:
        tracked = tracker.update(frame_detections)
        # tracked is a list[TrackedDetection] with .track_id added

Matches existing code style in detector.py / rules.py.
No new model weights. No GPU required (Kalman + Hungarian on CPU).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from scipy.optimize import linear_sum_assignment

from pipeline.detector import Detection, FrameDetections

logger = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────

# Classes that get tracked (others pass through with track_id = -1)
TRACKED_CLASSES: set[str] = {
    "person",
    "motorcycle",
    "bicycle",
    "car",
    "bus",
    "truck",
    "mini_lcv",
    "auto_rickshaw",
    "vehicle",
}
VEHICLE_TRACK_CLASSES: set[str] = TRACKED_CLASSES - {"person"}

# Minimum IoU to consider a detection-track pair a match
IOU_MATCH_THRESHOLD: float     = 0.3

# High-confidence threshold for Pass 1 matching (Pass 2 uses the rest)
HIGH_CONF_THRESHOLD: float      = 0.5

# Frames a track must be matched consecutively before it is "confirmed"
MIN_HITS: int                   = 2

# Max consecutive misses before a confirmed track is moved to "lost"
MAX_AGE: int                    = 30

# Max frames a lost track is kept before permanent deletion
LOST_TTL: int                   = 60


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class TrackedDetection:
    """
    A Detection annotated with a stable tracker ID.

    track_id == -1 means the detection class is not tracked
    (e.g. helmet, license_plate) and passes through unchanged.
    """
    class_name:  str
    confidence:  float
    bbox:        list[float]
    track_id:    int = -1


@dataclass
class _KalmanTrack:
    """
    Single-object track with a constant-velocity Kalman filter.

    State vector: [cx, cy, w, h, vx, vy, vw, vh]
      cx, cy  -- centre x, y  (pixels)
      w, h    -- width, height (pixels)
      vx, vy  -- velocity of centre
      vw, vh  -- rate of change of w, h (usually small)
    """
    track_id:    int
    class_name:  str

    # Kalman state (8-dim)
    x:           np.ndarray          # mean state
    P:           np.ndarray          # covariance

    # Lifecycle counters
    hits:        int   = 0           # consecutive matched frames
    age:         int   = 0           # total frames since creation
    time_since_last_match: int = 0   # consecutive unmatched frames
    is_confirmed: bool = False
    is_lost:      bool = False
    lost_age:     int  = 0           # frames in "lost" state

    # Last seen bbox (for IoU matching when filter diverges)
    last_bbox:   list[float] = field(default_factory=list)
    confidence:  float = 0.0

    @staticmethod
    def from_bbox(
        track_id: int,
        class_name: str,
        bbox: list[float],
        confidence: float = 0.0,
    ) -> "_KalmanTrack":
        """Initialise a new track from a detection bbox [x1,y1,x2,y2]."""
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        w  = x2 - x1
        h  = y2 - y1

        x = np.array([cx, cy, w, h, 0, 0, 0, 0], dtype=float)

        # Initial covariance — high uncertainty on velocities
        P = np.diag([w, h, w, h, 100, 100, 10, 10]).astype(float) ** 2

        return _KalmanTrack(
            track_id=track_id,
            class_name=class_name,
            x=x, P=P,
            hits=1, age=1,
            last_bbox=list(bbox),
            confidence=confidence,
        )

    def predict(self) -> list[float]:
        """Kalman predict step. Returns predicted bbox [x1,y1,x2,y2]."""
        # Transition matrix F: constant velocity model
        F = np.eye(8)
        F[0, 4] = 1.0   # cx += vx
        F[1, 5] = 1.0   # cy += vy
        F[2, 6] = 1.0   # w  += vw
        F[3, 7] = 1.0   # h  += vh

        # Process noise Q
        Q = np.diag([1.0, 1.0, 1.0, 1.0, 0.1, 0.1, 0.01, 0.01])

        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
        self.age += 1
        self.time_since_last_match += 1
        return self._state_to_bbox()

    def update(self, bbox: list[float], confidence: float | None = None) -> None:
        """Kalman update step with a matched detection bbox."""
        x1, y1, x2, y2 = bbox
        z = np.array([
            (x1 + x2) / 2.0,
            (y1 + y2) / 2.0,
            x2 - x1,
            y2 - y1,
        ])

        # Observation matrix H (observe cx, cy, w, h only)
        H = np.zeros((4, 8))
        H[0, 0] = H[1, 1] = H[2, 2] = H[3, 3] = 1.0

        # Measurement noise R
        w, h = z[2], z[3]
        R = np.diag([w * 0.1, h * 0.1, w * 0.1, h * 0.1]) ** 2

        S = H @ self.P @ H.T + R
        K = self.P @ H.T @ np.linalg.inv(S)
        y = z - H @ self.x
        self.x = self.x + K @ y
        self.P = (np.eye(8) - K @ H) @ self.P

        self.last_bbox = list(bbox)
        if confidence is not None:
            self.confidence = max(self.confidence * 0.7, confidence)
        self.hits += 1
        self.time_since_last_match = 0
        self.is_lost = False
        self.lost_age = 0
        if self.hits >= MIN_HITS:
            self.is_confirmed = True

    def _state_to_bbox(self) -> list[float]:
        cx, cy, w, h = self.x[0], self.x[1], self.x[2], self.x[3]
        w = max(w, 1.0)
        h = max(h, 1.0)
        return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]

    def predicted_bbox(self) -> list[float]:
        return self._state_to_bbox()


# ── IoU helpers ───────────────────────────────────────────────────────────────

def _iou(a: list[float], b: list[float]) -> float:
    """Compute IoU between two [x1,y1,x2,y2] boxes."""
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _iou_cost_matrix(
    detections: List[list[float]],
    tracks: List[_KalmanTrack],
) -> np.ndarray:
    """
    Build (N_det × N_track) cost matrix where cost = 1 - IoU.
    Lower cost = better match (Hungarian minimises cost).
    """
    n_det   = len(detections)
    n_track = len(tracks)
    cost    = np.ones((n_det, n_track), dtype=float)
    for i, det_bbox in enumerate(detections):
        for j, trk in enumerate(tracks):
            iou = _iou(det_bbox, trk.predicted_bbox())
            cost[i, j] = 1.0 - iou
    return cost


def _hungarian_match(
    det_bboxes: List[list[float]],
    tracks: List[_KalmanTrack],
    threshold: float,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """
    Run Hungarian assignment.

    Returns:
        matches         -- list of (det_idx, track_idx) pairs above threshold
        unmatched_dets  -- det indices with no good match
        unmatched_trks  -- track indices with no good match
    """
    if not det_bboxes or not tracks:
        return [], list(range(len(det_bboxes))), list(range(len(tracks)))

    cost = _iou_cost_matrix(det_bboxes, tracks)
    row_ind, col_ind = linear_sum_assignment(cost)

    matches: list[tuple[int, int]] = []
    unmatched_dets = set(range(len(det_bboxes)))
    unmatched_trks = set(range(len(tracks)))

    for r, c in zip(row_ind, col_ind):
        if cost[r, c] <= (1.0 - threshold):        # IoU >= threshold
            matches.append((r, c))
            unmatched_dets.discard(r)
            unmatched_trks.discard(c)

    return matches, list(unmatched_dets), list(unmatched_trks)


# ── Main Tracker class ────────────────────────────────────────────────────────

class Tracker:
    """
    ByteTrack multi-object tracker.

    One instance per pipeline run (holds state across frames).
    Call update() once per frame in temporal order.

    Example
    -------
        tracker = Tracker()
        for fd in frame_detections:
            tracked = tracker.update(fd)
    """

    def __init__(self) -> None:
        self._next_id: int = 1
        self._active:  list[_KalmanTrack] = []
        self._lost:    list[_KalmanTrack] = []

    def _new_track(self, det: Detection) -> _KalmanTrack:
        t = _KalmanTrack.from_bbox(
            self._next_id,
            det.class_name,
            det.bbox,
            det.confidence,
        )
        self._next_id += 1
        return t

    def update(self, fd: FrameDetections) -> list[TrackedDetection]:
        """
        Process one frame's detections and return TrackedDetection list.

        Non-tracked classes (helmet, license_plate, …) pass through with
        track_id = -1. Tracked classes (person, motorcycle) get assigned a
        stable integer ID.
        """
        # Separate tracked vs pass-through detections
        tracked_dets:   list[Detection] = [d for d in fd.detections if d.class_name in TRACKED_CLASSES]
        passthrough_dets: list[Detection] = [d for d in fd.detections if d.class_name not in TRACKED_CLASSES]

        # Split tracked detections into high/low confidence
        high_dets = [d for d in tracked_dets if d.confidence >= HIGH_CONF_THRESHOLD]
        low_dets  = [d for d in tracked_dets if d.confidence <  HIGH_CONF_THRESHOLD]

        # Predict all active tracks forward one step
        for trk in self._active:
            trk.predict()

        # ── Pass 1: high-confidence dets vs active tracks ─────────────────────
        high_bboxes = [d.bbox for d in high_dets]
        matches1, unmatched_high, unmatched_active = _hungarian_match(
            high_bboxes, self._active, IOU_MATCH_THRESHOLD,
        )

        for det_idx, trk_idx in matches1:
            self._active[trk_idx].update(
                high_dets[det_idx].bbox,
                high_dets[det_idx].confidence,
            )

        # Unmatched active tracks → move to lost
        newly_lost: list[_KalmanTrack] = []
        for trk_idx in sorted(unmatched_active, reverse=True):
            trk = self._active.pop(trk_idx)
            trk.is_lost = True
            trk.lost_age = 1
            newly_lost.append(trk)
        self._lost.extend(newly_lost)

        # ── Pass 2: low-confidence dets vs lost tracks ────────────────────────
        low_bboxes = [d.bbox for d in low_dets]
        if low_bboxes and self._lost:
            matches2, _, _ = _hungarian_match(
                low_bboxes, self._lost, IOU_MATCH_THRESHOLD,
            )
            revived_lost_indices: set[int] = set()
            for det_idx, trk_idx in matches2:
                self._lost[trk_idx].update(
                    low_dets[det_idx].bbox,
                    low_dets[det_idx].confidence,
                )
                self._lost[trk_idx].is_lost = False
                self._lost[trk_idx].lost_age = 0
                self._active.append(self._lost[trk_idx])
                revived_lost_indices.add(trk_idx)
            self._lost = [t for i, t in enumerate(self._lost)
                          if i not in revived_lost_indices]

        # ── Spawn new tracks for unmatched high-conf dets ─────────────────────
        for det_idx in unmatched_high:
            new_trk = self._new_track(high_dets[det_idx])
            self._active.append(new_trk)

        # ── Prune expired lost tracks ──────────────────────────────────────────
        for trk in self._lost:
            trk.lost_age += 1
        self._lost = [t for t in self._lost if t.lost_age <= LOST_TTL]

        # ── Remove stale active tracks (too many misses) ──────────────────────
        self._active = [t for t in self._active
                        if t.time_since_last_match <= MAX_AGE]

        # ── Build output ───────────────────────────────────────────────────────
        output: list[TrackedDetection] = []

        # Pass-through detections (helmet, plate, etc.)
        for d in passthrough_dets:
            output.append(TrackedDetection(
                class_name=d.class_name,
                confidence=d.confidence,
                bbox=d.bbox,
                track_id=-1,
            ))

        # Confirmed + tentative tracked detections. Keep very recent predicted
        # boxes for confirmed tracks so annotated videos do not flicker when a
        # detector misses a frame.
        for trk in self._active:
            if trk.time_since_last_match == 0 or (trk.is_confirmed and trk.time_since_last_match <= 3):
                bbox = trk.last_bbox if trk.time_since_last_match == 0 else trk.predicted_bbox()
                decay = max(0.25, 1.0 - 0.2 * trk.time_since_last_match)
                output.append(TrackedDetection(
                    class_name=trk.class_name,
                    confidence=trk.confidence * decay,
                    bbox=bbox,
                    track_id=trk.track_id,
                ))

        n_confirmed = sum(1 for t in self._active if t.is_confirmed)
        logger.debug(
            "Frame %.3fs | active=%d confirmed=%d lost=%d | next_id=%d",
            fd.timestamp,
            len(self._active),
            n_confirmed,
            len(self._lost),
            self._next_id,
        )
        return output
