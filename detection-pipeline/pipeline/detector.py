"""
pipeline/detector.py
────────────────────
Three-model detection architecture for the RakshaRide pipeline.

Models
------
1. COCO_MODEL    (yolov8n.pt)         → person, motorcycle
2. HELMET_MODEL  (helmet_model.pt)    → helmet, no helmet
3. PLATE_MODEL   (ampr.pt)            → Number_plate → license_plate

All three models are lazy-loaded singletons (loaded once, reused every frame).
Results from all three are merged into a single FrameDetections object so the
rest of the pipeline never needs to know about the model boundary.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Model paths ───────────────────────────────────────────────────────────────

COCO_MODEL_PATH:   str           = os.environ.get("COCO_MODEL_PATH",   "yolov8n.pt")
HELMET_MODEL_PATH: str           = os.environ.get("HELMET_MODEL_PATH", "models/helmet_model.pt")
PLATE_MODEL_PATH:  Optional[str] = os.environ.get("PLATE_MODEL_PATH",  "models/ampr.pt")

# Confidence threshold for all models
DETECTION_CONF_THRESHOLD: float = float(os.environ.get("YOLO_CONF_THRESHOLD", "0.35"))

# ── Class maps (raw model label → internal normalised label) ──────────────────

COCO_CLASSES: Dict[str, str] = {
    "person":     "person",
    "motorcycle": "motorcycle",
    "motorbike":  "motorcycle",
}

HELMET_CLASSES: Dict[str, str] = {
    "helmet":    "helmet",
    "no helmet": "no_helmet",
    "no_helmet": "no_helmet",
}

PLATE_CLASSES: Dict[str, str] = {
    "number_plate":  "license_plate",
    "numberplate":   "license_plate",
    "license_plate": "license_plate",
    "plate":         "license_plate",
}


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Detection:
    """Single bounding-box detection. bbox = [x1, y1, x2, y2] pixels."""
    class_name: str
    confidence: float
    bbox: List[float]


@dataclass
class FrameDetections:
    """All merged detections for one frame."""
    timestamp: float
    detections: List[Detection] = field(default_factory=list)

    def by_class(self, cls: str) -> List[Detection]:
        return [d for d in self.detections if d.class_name == cls]


# ── Singleton model registry ──────────────────────────────────────────────────

_coco_model   = None
_helmet_model = None
_plate_model  = None


def _load(path: str, label: str):
    from ultralytics import YOLO
    if not os.path.exists(path) and path != "yolov8n.pt":
        logger.warning("Model file not found: %s — skipping %s detection", path, label)
        return None
    logger.info("Loading %s model: %s", label, path)
    m = YOLO(path)
    logger.info("%s model ready. Classes: %s", label, list(m.names.values()))
    return m


def _get_coco():
    global _coco_model
    if _coco_model is None:
        _coco_model = _load(COCO_MODEL_PATH, "COCO/person+motorcycle")
    return _coco_model


def _get_helmet():
    global _helmet_model
    if _helmet_model is None:
        _helmet_model = _load(HELMET_MODEL_PATH, "helmet")
    return _helmet_model


def _get_plate():
    global _plate_model
    if _plate_model is None:
        if PLATE_MODEL_PATH:
            _plate_model = _load(PLATE_MODEL_PATH, "plate")
    return _plate_model


# ── Inference helper ──────────────────────────────────────────────────────────

def _infer(model, rgb: np.ndarray, class_map: Dict[str, str]) -> List[Detection]:
    """Run one model and return mapped Detection objects."""
    if model is None:
        return []
    dets: List[Detection] = []
    for result in model(rgb, verbose=False, conf=DETECTION_CONF_THRESHOLD):
        if result.boxes is None:
            continue
        for box in result.boxes:
            raw = result.names[int(box.cls[0])].lower().strip()
            norm = raw.replace(" ", "_").replace("-", "_")
            mapped = class_map.get(raw) or class_map.get(norm)
            if mapped is None:
                continue
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            dets.append(Detection(mapped, float(box.conf[0]), [x1, y1, x2, y2]))
    return dets


# ── Public API ────────────────────────────────────────────────────────────────

def detect_frame(frame_image: np.ndarray, timestamp: float = 0.0) -> FrameDetections:
    """
    Run all three models on one BGR frame and return merged detections.
    Load order: COCO → Helmet → Plate (each loaded once, cached as singleton).
    """
    rgb = frame_image[:, :, ::-1]   # BGR → RGB for ultralytics
    fd  = FrameDetections(timestamp=timestamp)

    # 1. Person + motorcycle
    fd.detections.extend(_infer(_get_coco(), rgb, COCO_CLASSES))

    # 2. Helmet / no-helmet
    fd.detections.extend(_infer(_get_helmet(), rgb, HELMET_CLASSES))

    # 3. License plate (ampr.pt wins over any plate from COCO)
    plate_dets = _infer(_get_plate(), rgb, PLATE_CLASSES)
    if plate_dets:
        fd.detections = [d for d in fd.detections if d.class_name != "license_plate"]
        fd.detections.extend(plate_dets)

    logger.debug(
        "Frame %.3fs → %d detections: %s",
        timestamp,
        len(fd.detections),
        [(d.class_name, f"{d.confidence:.2f}") for d in fd.detections],
    )
    return fd


def detect_frames(frames) -> List[FrameDetections]:
    return [detect_frame(f.image, f.timestamp) for f in frames]
