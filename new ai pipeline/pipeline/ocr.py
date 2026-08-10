"""
pipeline/ocr.py
---------------
EasyOCR wrapper for license plate reading with Indian format validation.

The reader is a module-level singleton; each instantiation takes 2-4 s so
loading it once per process is intentional.

Plate format: ^[A-Z]{2}[0-9]{2}[A-Z]{1,2}[0-9]{4}$
  Examples: MH12AB1234, DL3CAB1234, KA05MG2765
Reads that don't match are discarded, not force-fit.
majority_vote_plate() selects the most common valid read across all frames.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

PLATE_REGEX  = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{1,2}\d{4}$")
OCR_LANGUAGES: List[str] = ["en"]

_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        try:
            import easyocr
        except ImportError as exc:
            raise ImportError("easyocr is not installed. Run: pip install easyocr") from exc
        logger.info("Initialising EasyOCR reader (languages=%s)...", OCR_LANGUAGES)
        _reader = easyocr.Reader(OCR_LANGUAGES, gpu=False, verbose=False)
        logger.info("EasyOCR reader ready.")
    return _reader


@dataclass
class PlateRead:
    """OCR result for one license-plate crop from one frame."""
    timestamp: float
    raw_text: str           # cleaned to uppercase alphanumeric
    is_valid_format: bool   # True if matches PLATE_REGEX
    ocr_confidence: float   # mean confidence from EasyOCR


def _clean(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _crop_bbox(image: np.ndarray, bbox: List[float]) -> Optional[np.ndarray]:
    """Crop [x1,y1,x2,y2] from image. Returns None if the crop area is zero."""
    x1, y1, x2, y2 = (int(v) for v in bbox)
    h, w = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return image[y1:y2, x1:x2]


def read_plate(
    image: np.ndarray,
    plate_bbox: List[float],
    timestamp: float = 0.0,
) -> Optional[PlateRead]:
    """
    Crop the plate region from image and run EasyOCR.

    Returns None if the crop is invalid or EasyOCR returns no text.
    """
    crop = _crop_bbox(image, plate_bbox)
    if crop is None:
        return None

    try:
        results = _get_reader().readtext(crop, detail=1, paragraph=False)
    except Exception as exc:
        logger.warning("EasyOCR error at t=%.3fs: %s", timestamp, exc)
        return None

    if not results:
        return None

    combined = "".join(r[1] for r in results)
    avg_conf = sum(r[2] for r in results) / len(results)
    cleaned  = _clean(combined)

    if not cleaned:
        return None

    return PlateRead(
        timestamp=timestamp,
        raw_text=cleaned,
        is_valid_format=bool(PLATE_REGEX.match(cleaned)),
        ocr_confidence=avg_conf,
    )


def majority_vote_plate(reads: List[PlateRead]) -> tuple[Optional[str], float]:
    """
    Return the most common format-valid plate string across all frames,
    and the fraction of valid reads that agree with it.

    Returns (None, 0.0) if no valid reads exist.
    """
    valid = [r for r in reads if r.is_valid_format]
    if not valid:
        return None, 0.0

    counter: dict[str, int] = {}
    for r in valid:
        counter[r.raw_text] = counter.get(r.raw_text, 0) + 1

    winner    = max(counter, key=lambda k: counter[k])
    agreement = counter[winner] / len(valid)
    logger.info("Plate: '%s' (%d/%d valid reads, agreement=%.2f)", winner, counter[winner], len(valid), agreement)
    return winner, agreement
