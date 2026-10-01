"""
pipeline/tracker_botsort.py
---------------------------
BoT-SORT tracking, driven directly, with ONE tracker per class group.

Why not `model.track()`: Ultralytics associates detections to tracks without
regard to class. On a real jam clip the motorcycle passing between cars
inherited a different car's id in almost every frame (ids 13, 11, 10, 6, 5, 3 …
for one physical bike), because its box overlapped whichever car it was passing.
Class-agnostic association is the single biggest identity error on dashcam
footage, and it is invisible until you trace one object frame by frame.

Here the detector runs ONCE per frame, the detections are split into class
groups (two-wheelers, four-wheelers), and each group has its own BoT-SORT
instance with its own Kalman states and its own camera-motion compensation.
A motorcycle can therefore never take a car's id. Ids are offset per group so
they stay globally unique.

Camera-motion compensation (sparse optical flow) is on: the camera is inside a
moving car. Appearance re-identification is off on this path — the ReID encoder
needs the predictor's feature maps, which are not available when the tracker is
driven with pre-computed boxes. Motion + IoU + GMC only, which is honest about
what it does; appearance ReID is a measurable future lever, not a claim.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Sequence

import numpy as np

from pipeline.detector import Detection, DEFAULT_IMGSZ, detect_vehicles
from pipeline.tracker import TrackedDetection

logger = logging.getLogger(__name__)

CFG_PATH = Path(__file__).parent / "botsort.yaml"

TWO_WHEELER = frozenset({"motorcycle", "bicycle"})
FOUR_WHEELER = frozenset({"car", "bus", "truck", "mini_lcv", "auto_rickshaw", "vehicle"})
# group name -> (classes, id offset). Offsets keep ids unique across groups.
GROUPS: Dict[str, tuple] = {"two": (TWO_WHEELER, 0), "four": (FOUR_WHEELER, 100_000)}

_DEFAULTS = dict(track_high_thresh=0.30, track_low_thresh=0.10, new_track_thresh=0.30,
                 track_buffer=45, match_thresh=0.80, fuse_score=True, gmc_method="sparseOptFlow",
                 proximity_thresh=0.50, appearance_thresh=0.75, with_reid=False, model="auto")


def _load_args(frame_rate: float) -> SimpleNamespace:
    cfg = dict(_DEFAULTS)
    try:
        import yaml
        loaded = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}
        cfg.update({k: v for k, v in loaded.items() if k in _DEFAULTS})
    except Exception as e:                         # a missing/broken yaml must not stop a run
        logger.warning("botsort.yaml not loaded (%s); using built-in defaults", e)
    cfg["with_reid"] = False                       # no encoder on this path — see module docstring
    cfg["frame_rate"] = int(max(1, round(frame_rate)))
    return SimpleNamespace(**cfg)


class _Dets:
    """Minimal detections view with the attributes BYTETracker.update() needs."""

    def __init__(self, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray):
        self.xyxy, self.conf, self.cls = xyxy, conf, cls

    def __len__(self) -> int:
        return int(self.xyxy.shape[0])

    def __getitem__(self, mask):
        return _Dets(self.xyxy[mask], self.conf[mask], self.cls[mask])

    @property
    def xywh(self) -> np.ndarray:
        if len(self) == 0:
            return np.empty((0, 4), dtype=np.float32)
        x1, y1, x2, y2 = self.xyxy[:, 0], self.xyxy[:, 1], self.xyxy[:, 2], self.xyxy[:, 3]
        return np.stack([(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1], axis=1).astype(np.float32)


class BotSortTracker:
    """
    Per-frame API:  tracked = tracker.update(frame_bgr, first=(i == 0))

    Returns TrackedDetection list: tracked vehicles carry a stable track_id,
    everything else (persons, plates, helmets) passes through with track_id = -1.
    """

    def __init__(self, imgsz: int = DEFAULT_IMGSZ, frame_rate: float = 15.0) -> None:
        self.imgsz = imgsz
        self.frame_rate = frame_rate
        self._trackers: Dict[str, object] = {}
        self._build()

    def _build(self) -> None:
        from ultralytics.trackers import BOTSORT
        args = _load_args(self.frame_rate)
        self._trackers = {name: BOTSORT(args) for name in GROUPS}

    def reset(self) -> None:
        self._build()

    # ── one frame ────────────────────────────────────────────────────────────
    def update(self, bgr: np.ndarray, first: bool = False,
               detections: Sequence[Detection] | None = None) -> List[TrackedDetection]:
        if first:
            self.reset()
        dets = list(detections) if detections is not None else detect_vehicles(bgr, imgsz=self.imgsz)
        out: List[TrackedDetection] = []
        claimed: set[int] = set()

        for name, (classes, offset) in GROUPS.items():
            idx = [i for i, d in enumerate(dets) if d.class_name in classes]
            tracker = self._trackers[name]
            if not idx:
                # keep the tracker's clock moving so lost tracks age correctly
                tracker.update(_Dets(np.empty((0, 4), np.float32), np.empty((0,), np.float32),
                                     np.empty((0,), np.float32)), bgr)
                continue
            view = _Dets(np.array([dets[i].bbox for i in idx], dtype=np.float32),
                         np.array([dets[i].confidence for i in idx], dtype=np.float32),
                         np.arange(len(idx), dtype=np.float32))     # cls = position within this group
            try:
                rows = tracker.update(view, bgr)
            except Exception as e:
                logger.warning("BoT-SORT group %r failed on this frame (%s); passing detections through untracked", name, e)
                rows = np.empty((0, 8), dtype=np.float32)
            for row in np.atleast_2d(rows):
                if row.size < 8:
                    continue
                local = int(row[7])                                  # idx back into `view`
                if local < 0 or local >= len(idx):
                    continue
                src = dets[idx[local]]
                claimed.add(idx[local])
                out.append(TrackedDetection(src.class_name, float(row[5]),
                                            [float(row[0]), float(row[1]), float(row[2]), float(row[3])],
                                            int(row[4]) + offset))

        # untracked classes (person, and any vehicle box the tracker did not confirm yet)
        for i, d in enumerate(dets):
            if i not in claimed:
                out.append(TrackedDetection(d.class_name, d.confidence, list(d.bbox), -1))
        return out


def vehicles_and_persons(tracked: List[TrackedDetection]) -> tuple[List[Detection], List[Detection]]:
    vehicles = [Detection(d.class_name, d.confidence, list(d.bbox)) for d in tracked
                if d.class_name in TWO_WHEELER or d.class_name in FOUR_WHEELER]
    persons = [Detection(d.class_name, d.confidence, list(d.bbox)) for d in tracked if d.class_name == "person"]
    return vehicles, persons


VEHICLE_CLASSES_ALL = TWO_WHEELER | FOUR_WHEELER
