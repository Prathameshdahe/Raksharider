"""
pipeline/detector.py
────────────────────
YOLOv8 wrapper (via ultralytics).

Design notes
------------
* The model is loaded ONCE when this module is first imported, not per-call.
  This is intentional — model load takes ~1-2 s and would kill throughput in
  an API setting.
* To swap in a fine-tuned model, change MODEL_PATH to your .pt file path.
  Everything else stays the same.
* Class names are mapped by the string labels returned by ultralytics so the
  code is robust to re-ordering of COCO indices.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Model config ─────────────────────────────────────────────────────────────

# Default to fine-tuned helmet model if available, fallback to yolov8n.pt
_DEFAULT_MODEL = "models/helmet_model.pt" if os.path.exists("models/helmet_model.pt") else "yolov8n.pt"
MODEL_PATH: str = os.environ.get("YOLO_MODEL_PATH", _DEFAULT_MODEL)

# Classes we care about. Lower-case strings mapped to normalized internal labels.
TARGET_CLASSES: Dict[str, str] = {
    "person": "person",
    "motorcycle": "motorcycle",
    "motorbike": "motorcycle",
    "helmet": "helmet",
    "no helmet": "no_helmet",
    "no_helmet": "no_helmet",
    "no-helmet": "no_helmet",
    "license_plate": "license_plate",
    "plate": "license_plate",
}

# Confidence threshold below which detections are discarded entirely.
DETECTION_CONF_THRESHOLD: float = float(os.environ.get("YOLO_CONF_THRESHOLD", "0.35"))


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Detection:
    """
    A single object detection result.

    bbox is [x1, y1, x2, y2] in absolute pixel coordinates.
    """

    class_name: str           # normalised class label (see TARGET_CLASSES)
    confidence: float         # 0.0 – 1.0
    bbox: List[float]         # [x1, y1, x2, y2]


@dataclass
class FrameDetections:
    """All detections for one frame."""

    timestamp: float
    detections: List[Detection] = field(default_factory=list)

    # Convenience accessors
    def by_class(self, cls: str) -> List[Detection]:
        return [d for d in self.detections if d.class_name == cls]


# ── Lazy model loading ────────────────────────────────────────────────────────

_model = None  # module-level singleton


def _get_model():
    """Return (and lazily load) the YOLO model singleton."""
    global _model
    if _model is None:
        try:
            from ultralytics import YOLO  # imported here to keep startup fast
        except ImportError as exc:
            raise ImportError(
                "ultralytics is not installed. Run: pip install ultralytics"
            ) from exc

        logger.info("Loading YOLO model from: %s", MODEL_PATH)
        _model = YOLO(MODEL_PATH)
        logger.info(
            "YOLO model loaded. Classes available: %s",
            list(_model.names.values()),
        )
    return _model


# ── Public API ────────────────────────────────────────────────────────────────

def detect_frame(frame_image: np.ndarray, timestamp: float = 0.0) -> FrameDetections:
    """
    Run YOLO inference on a single BGR numpy array.

    Parameters
    ----------
    frame_image:
        BGR image as returned by OpenCV or frame_extractor.
    timestamp:
        Source timestamp (seconds) — passed through for traceability.

    Returns
    -------
    FrameDetections with all detections above DETECTION_CONF_THRESHOLD
    whose class_name is in TARGET_CLASSES.
    """
    model = _get_model()

    # ultralytics expects RGB; convert from BGR
    rgb = frame_image[:, :, ::-1]

    results = model(rgb, verbose=False, conf=DETECTION_CONF_THRESHOLD)

    fd = FrameDetections(timestamp=timestamp)

    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue

        names: Dict[int, str] = result.names  # {0: 'person', 1: 'bicycle', ...}

        for box in boxes:
            cls_id = int(box.cls[0].item())
            raw_label = names.get(cls_id, "unknown").lower().replace(" ", "_")

            # Only keep classes we care about
            if raw_label not in TARGET_CLASSES:
                continue

            conf = float(box.conf[0].item())
            x1, y1, x2, y2 = box.xyxy[0].tolist()

            fd.detections.append(
                Detection(
                    class_name=TARGET_CLASSES[raw_label],
                    confidence=conf,
                    bbox=[x1, y1, x2, y2],
                )
            )

    logger.debug(
        "Frame %.3fs — %d detections: %s",
        timestamp,
        len(fd.detections),
        [(d.class_name, f"{d.confidence:.2f}") for d in fd.detections],
    )
    return fd


def detect_frames(frames) -> List[FrameDetections]:
    """
    Convenience wrapper: run detect_frame over a list of Frame objects
    (as returned by frame_extractor.extract_frames).
    """
    results = []
    for f in frames:
        fd = detect_frame(f.image, f.timestamp)
        results.append(fd)
    return results
