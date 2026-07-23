"""
pipeline/detector.py
────────────────────
Dual-model YOLOv8 detection pipeline.

Models
------
1. HELMET_MODEL  (models/helmet_model.pt)
   Fine-tuned on helmet dataset. Detects: helmet, no helmet.
   Also detects person + motorcycle from COCO base weights.

2. PLATE_MODEL   (models/ampr.pt)
   Dedicated license-plate detector. Class: Number_plate.
   Used exclusively to locate the plate bounding box region for EasyOCR.

Design notes
------------
* Both models are loaded ONCE at first use (lazy singleton) — not per call.
* Plate detections from ampr.pt are normalised to the internal label
  "license_plate" so the rest of the pipeline is model-agnostic.
* To swap either model, change the path constants below — nothing else changes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Model paths ───────────────────────────────────────────────────────────────

# Primary model: helmet + person + motorcycle
_DEFAULT_HELMET_MODEL = (
    "models/helmet_model.pt"
    if os.path.exists("models/helmet_model.pt")
    else "yolov8n.pt"
)
HELMET_MODEL_PATH: str = os.environ.get("YOLO_MODEL_PATH", _DEFAULT_HELMET_MODEL)

# Dedicated license plate detector (ampr.pt)
_DEFAULT_PLATE_MODEL = (
    "models/ampr.pt"
    if os.path.exists("models/ampr.pt")
    else None
)
PLATE_MODEL_PATH: Optional[str] = os.environ.get("PLATE_MODEL_PATH", _DEFAULT_PLATE_MODEL)

# Confidence threshold below which detections are discarded
DETECTION_CONF_THRESHOLD: float = float(os.environ.get("YOLO_CONF_THRESHOLD", "0.35"))

# ── Class name mappings ───────────────────────────────────────────────────────

# Classes we care about from the helmet/person model
HELMET_TARGET_CLASSES: Dict[str, str] = {
    "person":      "person",
    "motorcycle":  "motorcycle",
    "motorbike":   "motorcycle",
    "helmet":      "helmet",
    "no helmet":   "no_helmet",
    "no_helmet":   "no_helmet",
    "no-helmet":   "no_helmet",
    # keep plate labels if base model also returns them
    "license_plate": "license_plate",
    "plate":         "license_plate",
}

# ampr.pt returns "Number_plate" — map to our internal label
PLATE_TARGET_CLASSES: Dict[str, str] = {
    "number_plate": "license_plate",
    "numberplate":  "license_plate",
    "plate":        "license_plate",
    "license_plate":"license_plate",
}


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Detection:
    """
    A single object detection result.
    bbox is [x1, y1, x2, y2] in absolute pixel coordinates.
    """
    class_name: str       # normalised label
    confidence: float     # 0.0 – 1.0
    bbox: List[float]     # [x1, y1, x2, y2]


@dataclass
class FrameDetections:
    """All detections for one frame."""
    timestamp: float
    detections: List[Detection] = field(default_factory=list)

    def by_class(self, cls: str) -> List[Detection]:
        return [d for d in self.detections if d.class_name == cls]


# ── Lazy model singletons ─────────────────────────────────────────────────────

_helmet_model = None
_plate_model  = None


def _get_helmet_model():
    global _helmet_model
    if _helmet_model is None:
        from ultralytics import YOLO
        logger.info("Loading helmet/person model: %s", HELMET_MODEL_PATH)
        _helmet_model = YOLO(HELMET_MODEL_PATH)
        logger.info("Helmet model classes: %s", list(_helmet_model.names.values()))
    return _helmet_model


def _get_plate_model():
    global _plate_model
    if _plate_model is None:
        if not PLATE_MODEL_PATH:
            logger.warning(
                "No plate model configured (ampr.pt not found). "
                "Plate detection will be skipped."
            )
            return None
        from ultralytics import YOLO
        logger.info("Loading plate detection model: %s", PLATE_MODEL_PATH)
        _plate_model = YOLO(PLATE_MODEL_PATH)
        logger.info("Plate model classes: %s", list(_plate_model.names.values()))
    return _plate_model


# ── Internal inference helper ─────────────────────────────────────────────────

def _run_model(
    model,
    rgb_image: np.ndarray,
    class_map: Dict[str, str],
    timestamp: float,
) -> List[Detection]:
    """Run a YOLO model and return filtered Detection objects."""
    detections: List[Detection] = []

    results = model(rgb_image, verbose=False, conf=DETECTION_CONF_THRESHOLD)
    for result in results:
        if result.boxes is None:
            continue
        names: Dict[int, str] = result.names
        for box in result.boxes:
            cls_id    = int(box.cls[0].item())
            raw_label = names.get(cls_id, "unknown").lower().strip()

            # Normalise spaces/dashes for lookup
            normalised = raw_label.replace(" ", "_").replace("-", "_")
            mapped = class_map.get(raw_label) or class_map.get(normalised)
            if mapped is None:
                continue

            conf = float(box.conf[0].item())
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            detections.append(Detection(class_name=mapped, confidence=conf,
                                        bbox=[x1, y1, x2, y2]))
    return detections


# ── Public API ────────────────────────────────────────────────────────────────

def detect_frame(frame_image: np.ndarray, timestamp: float = 0.0) -> FrameDetections:
    """
    Run both models on a single BGR frame and merge results.

    - Helmet model  → person, motorcycle, helmet, no_helmet
    - Plate model   → license_plate  (ampr.pt)

    Returns a single FrameDetections with all detections combined.
    """
    rgb = frame_image[:, :, ::-1]   # BGR → RGB

    fd = FrameDetections(timestamp=timestamp)

    # ── Helmet / person / motorcycle detections ───────────────────────────
    helmet_dets = _run_model(
        _get_helmet_model(), rgb, HELMET_TARGET_CLASSES, timestamp
    )
    fd.detections.extend(helmet_dets)

    # ── License plate detections (ampr.pt) ────────────────────────────────
    plate_model = _get_plate_model()
    if plate_model is not None:
        plate_dets = _run_model(plate_model, rgb, PLATE_TARGET_CLASSES, timestamp)
        # Avoid double-counting if helmet model also returned a plate
        if plate_dets:
            # Remove any plates already found by helmet model, prefer ampr.pt
            fd.detections = [d for d in fd.detections if d.class_name != "license_plate"]
            fd.detections.extend(plate_dets)

    logger.debug(
        "Frame %.3fs — %d total detections: %s",
        timestamp,
        len(fd.detections),
        [(d.class_name, f"{d.confidence:.2f}") for d in fd.detections],
    )
    return fd


def detect_frames(frames) -> List[FrameDetections]:
    """
    Run detect_frame over a list of Frame objects
    (as returned by frame_extractor.extract_frames).
    """
    return [detect_frame(f.image, f.timestamp) for f in frames]
