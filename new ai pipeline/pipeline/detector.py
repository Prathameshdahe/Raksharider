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
PLATE_TARGET_CLASSES = PLATE_CLASSES


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

DEFAULT_IMGSZ: int = int(os.environ.get("YOLO_IMGSZ", "1280"))   # full-frame vehicle/person detection
ROI_IMGSZ: int     = int(os.environ.get("YOLO_ROI_IMGSZ", "640"))  # zoomed-crop inference


def _infer(model, bgr: np.ndarray, class_map: dict[str, str],
           conf_override: float | None = None, imgsz: int | None = None) -> list[Detection]:
    """
    Run one model on a BGR frame and return mapped Detection objects.

    Ultralytics expects NumPy input in OpenCV's native BGR order and swaps
    channels itself. A/B on 20 frames of tests/sample_videos/sample-3.mp4
    (ultralytics 8.4.19):  COCO  BGR 304 dets @0.499 vs RGB 310 @0.491 (wash);
    plate model BGR 8 @0.391 vs RGB 5 @0.374 (BGR clearly better). So: no swap.
    """
    if model is None:
        return []
    conf = conf_override if conf_override is not None else DETECTION_CONF_THRESHOLD
    dets: list[Detection] = []
    kwargs = {"verbose": False, "conf": conf}
    if imgsz:
        kwargs["imgsz"] = imgsz
    for result in model(bgr, **kwargs):
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


# ── Deduplication ────────────────────────────────────────────────────────────

def _dedup_detections(dets: list[Detection], iou_threshold: float = 0.70) -> list[Detection]:
    """
    Remove duplicate detections of the SAME class whose IoU exceeds iou_threshold.

    Ported from DashCop inference/instance_funcs.py remove_duplicate_masks(),
    adapted to bounding boxes instead of pixel masks.

    Rationale: when two models (e.g. COCO + helmet model) both fire on the
    same physical person, the merged list contains two 'person' boxes nearly
    identical in position.  Keeping both inflates rider_count and can trigger
    false triple-riding verdicts.

    Strategy: for each overlapping pair of same-class boxes, keep the one
    with HIGHER confidence and discard the other.  This is conservative
    (threshold = 0.70) so genuinely adjacent detections are never merged.
    """
    if len(dets) <= 1:
        return dets

    keep = [True] * len(dets)
    for i in range(len(dets)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(dets)):
            if not keep[j]:
                continue
            if dets[i].class_name != dets[j].class_name:
                continue
            if _bbox_iou(dets[i].bbox, dets[j].bbox) > iou_threshold:
                # Keep the higher-confidence detection
                if dets[i].confidence >= dets[j].confidence:
                    keep[j] = False
                else:
                    keep[i] = False
                    break   # i is gone, move to next i

    kept = [d for d, k in zip(dets, keep) if k]
    removed = len(dets) - len(kept)
    if removed > 0:
        logger.debug("Dedup removed %d duplicate detection(s) (IoU>%.2f)", removed, iou_threshold)
    return kept


def _bbox_iou(a: list[float], b: list[float]) -> float:
    """Standard IoU between two [x1,y1,x2,y2] boxes."""
    ix1 = max(a[0], b[0]);  iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]);  iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    area_a = max(0.0, a[2]-a[0]) * max(0.0, a[3]-a[1])
    area_b = max(0.0, b[2]-b[0]) * max(0.0, b[3]-b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


GENERIC_VEHICLE_CLASSES: frozenset = frozenset({"car", "bus", "truck", "vehicle", "mini_lcv"})
FUSION_IOU_THRESHOLD: float = 0.50


def fuse_specialist_vehicles(dets: list[Detection], specialist: list[Detection],
                             iou_threshold: float = FUSION_IOU_THRESHOLD) -> list[Detection]:
    """
    Merge classifiacation.pt output into the COCO list by spatial matching.

    For each specialist detection, the best-overlapping generic vehicle box
    (IoU >= threshold) is replaced by the specialist label. Unmatched specialist
    boxes are appended; unmatched generic boxes are KEPT. Never wipes a frame.
    """
    if not specialist:
        return dets
    generic_idx = [i for i, d in enumerate(dets) if d.class_name in GENERIC_VEHICLE_CLASSES]
    replaced: set[int] = set()
    out = list(dets)
    for sp in specialist:
        best_i, best_iou = -1, iou_threshold
        for i in generic_idx:
            if i in replaced:
                continue
            iou = _bbox_iou(dets[i].bbox, sp.bbox)
            if iou >= best_iou:
                best_i, best_iou = i, iou
        if best_i >= 0:
            replaced.add(best_i)
            out[best_i] = Detection(sp.class_name, max(sp.confidence, dets[best_i].confidence), sp.bbox)
        else:
            out.append(sp)
    return out


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
    bgr = frame_image                # native OpenCV order — see _infer() docstring
    fd  = FrameDetections(timestamp=timestamp)

    # 1. Person + motorcycle + broad vehicle classes from COCO
    #    Lower threshold so riders at distance / partial views are captured
    fd.detections.extend(_infer(_get_coco(), bgr, COCO_CLASSES,
                                conf_override=COCO_CONF_THRESHOLD))

    # 2. Helmet / no-helmet (separate conf — stricter to reduce false positives)
    fd.detections.extend(_infer(_get_helmet(), bgr, HELMET_CLASSES,
                                conf_override=HELMET_CONF_THRESHOLD))

    # 3. License plate (ampr.pt wins over any plate from COCO)
    plate_dets = _infer(_get_plate(), bgr, PLATE_CLASSES,
                        conf_override=PLATE_CONF_THRESHOLD)
    if plate_dets:
        fd.detections = [d for d in fd.detections if d.class_name != "license_plate"]
        fd.detections.extend(plate_dets)

    # 4. Fine-grained vehicle classification (classifiacation.pt)
    #    Spatial fusion: a specialist box REPLACES only the generic COCO box it
    #    overlaps. Generic vehicles the specialist never saw survive.
    vehicle_dets = _infer(_get_vehicle_class(), bgr, VEHICLE_CLASSES,
                          conf_override=VEHICLE_CONF_THRESHOLD)
    fd.detections = fuse_specialist_vehicles(fd.detections, vehicle_dets)

    logger.debug(
        "Frame %.3fs → %d detections (after dedup): %s",
        timestamp,
        len(fd.detections),
        [(d.class_name, f"{d.confidence:.2f}") for d in fd.detections],
    )

    # ── Deduplication (DashCop remove_duplicate_masks pattern) ────────────────
    # Remove same-class boxes with IoU > 0.70. Prevents double-counting when
    # two models fire on the same physical object (e.g. COCO + helmet model
    # both detecting the same person).
    fd.detections = _dedup_detections(fd.detections, iou_threshold=0.70)

    return fd


def detect_frames(frames) -> list[FrameDetections]:
    return [detect_frame(f.image, f.timestamp) for f in frames]


# ── v3: full-frame vehicles + zoomed-crop small objects ──────────────────────

TWO_WHEELER_ROI_CLASSES = frozenset({"motorcycle", "bicycle"})


def model_versions() -> dict[str, str]:
    """Pinned identifiers for the result package (file names + library version)."""
    try:
        import ultralytics
        ul = ultralytics.__version__
    except Exception:
        ul = "unknown"
    return {
        "ultralytics": ul,
        "coco": os.path.basename(COCO_MODEL_PATH),
        "helmet": os.path.basename(HELMET_MODEL_PATH),
        "plate": os.path.basename(PLATE_MODEL_PATH or ""),
        "vehicle_class": os.path.basename(VEHICLE_CLASS_MODEL_PATH or ""),
    }


def detect_vehicles(frame_image: np.ndarray, imgsz: int = DEFAULT_IMGSZ) -> list[Detection]:
    """COCO (person + vehicles + traffic light + phone) at full-frame resolution, specialist classes fused in."""
    dets = _infer(_get_coco(), frame_image, COCO_CLASSES, conf_override=COCO_CONF_THRESHOLD, imgsz=imgsz)
    spec = _infer(_get_vehicle_class(), frame_image, VEHICLE_CLASSES, conf_override=VEHICLE_CONF_THRESHOLD, imgsz=imgsz)
    return _dedup_detections(fuse_specialist_vehicles(dets, spec), iou_threshold=0.70)


def _crop_with_margin(img: np.ndarray, bbox: list[float], mx: float, my_top: float, my_bottom: float):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    if bw < 8 or bh < 8:
        return None
    cx1 = int(max(0, x1 - bw * mx)); cx2 = int(min(w, x2 + bw * mx))
    cy1 = int(max(0, y1 - bh * my_top)); cy2 = int(min(h, y2 + bh * my_bottom))
    if cx2 - cx1 < 8 or cy2 - cy1 < 8:
        return None
    return img[cy1:cy2, cx1:cx2], cx1, cy1


def _shift(dets: list[Detection], dx: int, dy: int) -> list[Detection]:
    return [Detection(d.class_name, d.confidence, [d.bbox[0] + dx, d.bbox[1] + dy, d.bbox[2] + dx, d.bbox[3] + dy]) for d in dets]


def detect_rois_owned(frame_image: np.ndarray, vehicles: list[Detection], persons: list[Detection],
                      imgsz: int = ROI_IMGSZ, min_vehicle_px: int = 40, min_person_px: int = 45
                      ) -> list[tuple[int | None, Detection]]:
    """
    Same as detect_rois(), but every detection is TAGGED with the index of the
    vehicle whose crop produced it (None for phones found in a person crop).

    This is what removes plate cross-assignment in dense traffic: a plate found
    inside vehicle V's own crop belongs to V by construction, so nothing has to
    be inferred from overlapping boxes afterwards. A plate that lands in the
    crop's padding (outside V's real box) is discarded rather than guessed.
    """
    out: list[tuple[int | None, Detection]] = []
    plate_m, helmet_m, coco_m = _get_plate(), _get_helmet(), _get_coco()
    for vi, v in enumerate(vehicles):
        if v.bbox[3] - v.bbox[1] < min_vehicle_px:
            continue
        c = _crop_with_margin(frame_image, v.bbox, 0.15, 0.10, 0.10)
        if c is not None and plate_m is not None:
            crop, dx, dy = c
            for d in _shift(_infer(plate_m, crop, PLATE_CLASSES, conf_override=PLATE_CONF_THRESHOLD, imgsz=imgsz), dx, dy):
                cx, cy = (d.bbox[0] + d.bbox[2]) / 2, (d.bbox[1] + d.bbox[3]) / 2
                if v.bbox[0] <= cx <= v.bbox[2] and v.bbox[1] <= cy <= v.bbox[3]:
                    out.append((vi, d))          # inside the real box → this vehicle owns it
        if v.class_name in TWO_WHEELER_ROI_CLASSES and helmet_m is not None:
            c2 = _crop_with_margin(frame_image, v.bbox, 0.25, 0.80, 0.05)
            if c2 is not None:
                crop, dx, dy = c2
                for d in _shift(_infer(helmet_m, crop, HELMET_CLASSES, conf_override=HELMET_CONF_THRESHOLD, imgsz=imgsz), dx, dy):
                    out.append((vi, d))
    if coco_m is not None:
        for p in persons:
            if p.bbox[3] - p.bbox[1] < min_person_px:
                continue
            c = _crop_with_margin(frame_image, p.bbox, 0.30, 0.20, 0.10)
            if c is None:
                continue
            crop, dx, dy = c
            for d in _shift(_infer(coco_m, crop, COCO_CLASSES, conf_override=COCO_CONF_THRESHOLD, imgsz=imgsz), dx, dy):
                if d.class_name == "cell_phone":
                    out.append((None, d))
    # de-duplicate while keeping each detection's owner
    kept = _dedup_detections([d for _, d in out], iou_threshold=0.70)
    kept_ids = {id(d) for d in kept}
    return [(o, d) for o, d in out if id(d) in kept_ids]


def detect_rois(frame_image: np.ndarray, vehicles: list[Detection], persons: list[Detection],
                imgsz: int = ROI_IMGSZ, min_vehicle_px: int = 40, min_person_px: int = 45) -> list[Detection]:
    """
    Run the small-object models on ZOOMED CROPS and map the boxes back to frame
    coordinates. A 4K frame downscaled to 640 turns a rider head into ~10 px; a
    crop of the vehicle letterboxed to 640 gives the helmet / plate / phone models
    the object scale they were trained on.

      plate model   -> every vehicle crop
      helmet model  -> two-wheeler crops extended upward (rider heads sit above the bike box)
      COCO (phone)  -> person crops
    """
    out: list[Detection] = []
    plate_m, helmet_m, coco_m = _get_plate(), _get_helmet(), _get_coco()
    for v in vehicles:
        if v.bbox[3] - v.bbox[1] < min_vehicle_px:
            continue
        c = _crop_with_margin(frame_image, v.bbox, 0.15, 0.10, 0.10)
        if c is not None and plate_m is not None:
            crop, dx, dy = c
            out += _shift(_infer(plate_m, crop, PLATE_CLASSES, conf_override=PLATE_CONF_THRESHOLD, imgsz=imgsz), dx, dy)
        if v.class_name in TWO_WHEELER_ROI_CLASSES and helmet_m is not None:
            c2 = _crop_with_margin(frame_image, v.bbox, 0.25, 0.80, 0.05)
            if c2 is not None:
                crop, dx, dy = c2
                out += _shift(_infer(helmet_m, crop, HELMET_CLASSES, conf_override=HELMET_CONF_THRESHOLD, imgsz=imgsz), dx, dy)
    if coco_m is not None:
        for p in persons:
            if p.bbox[3] - p.bbox[1] < min_person_px:
                continue
            c = _crop_with_margin(frame_image, p.bbox, 0.30, 0.20, 0.10)
            if c is None:
                continue
            crop, dx, dy = c
            phones = [d for d in _infer(coco_m, crop, COCO_CLASSES, conf_override=COCO_CONF_THRESHOLD, imgsz=imgsz)
                      if d.class_name == "cell_phone"]
            out += _shift(phones, dx, dy)
    return _dedup_detections(out, iou_threshold=0.70)
