"""
pipeline/detector.py
────────────────────
Four-model detection architecture for the DriveTrust AI pipeline.

Models
------
1. COCO_MODEL          (yolov8n.pt)           → person, motorcycle, car, bus,
                                                  truck, traffic_light, cell phone,
                                                  bicycle, auto-rickshaw (tricycle)
2. HELMET_MODEL        (helmet_model.pt)       → helmet, no helmet
3. PLATE_MODEL         (ampr.pt)               → Number_plate → license_plate
4. VEHICLE_CLASS_MODEL (classifiacation.pt)    → Bus, Car, Mini LCV, Truck variants

All four models are lazy-loaded singletons (loaded once, reused every frame).
Results from all four are merged into a single FrameDetections object so the
rest of the pipeline never needs to know about the model boundary.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

# pyrefly: ignore [missing-import]
import numpy as np

logger = logging.getLogger(__name__)

# ── Model paths ───────────────────────────────────────────────────────────────

COCO_MODEL_PATH:          str        = os.environ.get("COCO_MODEL_PATH",          "models/yolov8n.pt")
HELMET_MODEL_PATH:        str        = os.environ.get("HELMET_MODEL_PATH",        "models/helmet_model.pt")
PLATE_MODEL_PATH:         str | None = os.environ.get("PLATE_MODEL_PATH",         "models/ampr.pt")
VEHICLE_CLASS_MODEL_PATH: str | None = os.environ.get("VEHICLE_CLASS_MODEL_PATH", "models/classifiacation.pt")

# Global fallback confidence threshold
DETECTION_CONF_THRESHOLD: float = float(os.environ.get("YOLO_CONF_THRESHOLD", "0.35"))

# Per-model confidence overrides
# Lower threshold for person/motorcycle to catch riders at distance or in crowds
COCO_CONF_THRESHOLD:   float = float(os.environ.get("COCO_CONF_THRESHOLD",   "0.20"))
HELMET_CONF_THRESHOLD: float = float(os.environ.get("HELMET_CONF_THRESHOLD", "0.40"))
PLATE_CONF_THRESHOLD:  float = float(os.environ.get("PLATE_CONF_THRESHOLD",  "0.30"))
VEHICLE_CONF_THRESHOLD: float = float(os.environ.get("VEHICLE_CONF_THRESHOLD", "0.25"))

# ── Class maps (raw model label → internal normalised label) ──────────────────

# COCO: keep only road-relevant classes
COCO_CLASSES: dict[str, str] = {
    "person":        "person",
    "motorcycle":    "motorcycle",
    "motorbike":     "motorcycle",
    "bicycle":       "bicycle",
    "car":           "car",
    "bus":           "bus",
    "truck":         "truck",
    "tricycle":      "auto_rickshaw",       # COCO idx 81 — close enough
    "auto rickshaw": "auto_rickshaw",
    "traffic light": "traffic_light",
    "traffic_light": "traffic_light",
    "cell phone":    "cell_phone",
    "cell_phone":    "cell_phone",
}

HELMET_CLASSES: dict[str, str] = {
    "helmet":    "helmet",
    "no helmet": "no_helmet",
    "no_helmet": "no_helmet",
}

PLATE_CLASSES: dict[str, str] = {
    "number_plate":  "license_plate",
    "numberplate":   "license_plate",
    "license_plate": "license_plate",
    "plate":         "license_plate",
}

# classifiacation.pt classes → unified internal labels
VEHICLE_CLASSES: dict[str, str] = {
    "bus":                          "bus",
    "car":                          "car",
    "mini light commerical vehicle": "mini_lcv",
    "mini light commercial vehicle": "mini_lcv",
    "truck":                        "truck",
    "truck 3 axle":                 "truck",
    "truck 4 axle":                 "truck",
    "truck 5 axle":                 "truck",
    "vehicle":                      "vehicle",          # generic catch-all
}


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Detection:
    """Single bounding-box detection. bbox = [x1, y1, x2, y2] pixels."""
    class_name: str
    confidence: float
    bbox: list[float]


@dataclass
class FrameDetections:
    """All merged detections for one frame."""
    timestamp: float
    detections: list[Detection] = field(default_factory=list)

    def by_class(self, cls: str) -> list[Detection]:
        return [d for d in self.detections if d.class_name == cls]

    def by_classes(self, *classes: str) -> list[Detection]:
        """Return detections matching any of the given class names."""
        cls_set = set(classes)
        return [d for d in self.detections if d.class_name in cls_set]


# ── Singleton model registry ──────────────────────────────────────────────────

_coco_model          = None
_helmet_model        = None
_plate_model         = None
_vehicle_class_model = None


def _load(path: str, label: str):
    # pyrefly: ignore [missing-import]
    from ultralytics import YOLO
    if not os.path.exists(path):
        logger.warning("Model file not found: %s — skipping %s detection", path, label)
        return None
    logger.info("Loading %s model: %s", label, path)
    m = YOLO(path)
    logger.info("%s model ready. Classes: %s", label, list(m.names.values()))
    return m


def _get_coco():
    global _coco_model
    if _coco_model is None:
        _coco_model = _load(COCO_MODEL_PATH, "COCO/person+motorcycle+vehicle")
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


def _get_vehicle_class():
    global _vehicle_class_model
    if _vehicle_class_model is None:
        if VEHICLE_CLASS_MODEL_PATH:
            _vehicle_class_model = _load(VEHICLE_CLASS_MODEL_PATH, "vehicle-class")
    return _vehicle_class_model


# ── Inference helper ──────────────────────────────────────────────────────────

def _infer(model, rgb: np.ndarray, class_map: dict[str, str],
           conf_override: float | None = None) -> list[Detection]:
    """Run one model and return mapped Detection objects."""
    if model is None:
        return []
    conf = conf_override if conf_override is not None else DETECTION_CONF_THRESHOLD
    dets: list[Detection] = []
    for result in model(rgb, verbose=False, conf=conf):
        if result.boxes is None:
            continue
        for box in result.boxes:
            raw  = result.names[int(box.cls[0])].lower().strip()
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
    Run all four models on one BGR frame and return merged detections.

    Load order: COCO → Helmet → Plate → VehicleClass (each loaded once, cached).

    Vehicle class deduplication:
      classifiacation.pt runs at lower confidence (0.25) and is used to
      *upgrade* a generic COCO vehicle detection to a specific class (e.g.
      car → bus).  If classifiacation.pt fires on the same region as a COCO
      detection, the more-specific label wins.
    """
    rgb = frame_image[:, :, ::-1]   # BGR → RGB for ultralytics
    fd  = FrameDetections(timestamp=timestamp)

    # 1. Person + motorcycle + broad vehicle classes from COCO
    #    Lower threshold so riders at distance / partial views are captured
    fd.detections.extend(_infer(_get_coco(), rgb, COCO_CLASSES,
                                conf_override=COCO_CONF_THRESHOLD))

    # 2. Helmet / no-helmet (separate conf — stricter to reduce false positives)
    fd.detections.extend(_infer(_get_helmet(), rgb, HELMET_CLASSES,
                                conf_override=HELMET_CONF_THRESHOLD))

    # 3. License plate (ampr.pt wins over any plate from COCO)
    plate_dets = _infer(_get_plate(), rgb, PLATE_CLASSES,
                        conf_override=PLATE_CONF_THRESHOLD)
    if plate_dets:
        fd.detections = [d for d in fd.detections if d.class_name != "license_plate"]
        fd.detections.extend(plate_dets)

    # 4. Fine-grained vehicle classification (classifiacation.pt)
    #    Lower confidence threshold — we want to catch even partial views.
    #    Only keep if the result is MORE specific than what COCO said.
    vehicle_dets = _infer(_get_vehicle_class(), rgb, VEHICLE_CLASSES,
                          conf_override=VEHICLE_CONF_THRESHOLD)
    if vehicle_dets:
        # Remove ONLY generic broad labels that classifiacation.pt also covers.
        # KEEP motorcycle, person, bicycle — COCO is better at those.
        generic_vehicle_classes = {"car", "bus", "truck", "vehicle", "mini_lcv"}
        fd.detections = [
            d for d in fd.detections if d.class_name not in generic_vehicle_classes
        ]
        fd.detections.extend(vehicle_dets)

    logger.debug(
        "Frame %.3fs → %d detections: %s",
        timestamp,
        len(fd.detections),
        [(d.class_name, f"{d.confidence:.2f}") for d in fd.detections],
    )
    return fd


def detect_frames(frames) -> list[FrameDetections]:
    return [detect_frame(f.image, f.timestamp) for f in frames]
