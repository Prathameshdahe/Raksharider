"""
pipeline/ocr.py
───────────────
EasyOCR wrapper with Indian license-plate format validation.

Design choices
--------------
* EasyOCR reader is instantiated once (module-level singleton) to avoid
  reloading language models per frame — each instantiation is ~2–4 s.
* We crop the plate region from the frame image before passing to OCR;
  a tightly cropped region gives far better accuracy than full-frame OCR.
* Plate format: ^[A-Z]{2}[0-9]{2}[A-Z]{1,2}[0-9]{4}$  (same as PLATE_REGEX below)
  Example valid plates: MH12AB1234, DL3CAB1234, KA05MG2765
  Plates that don't match are recorded as invalid (not force-fit).
* Majority-vote across all format-valid reads across all frames gives the
  final plate string.  If no read is format-valid, returns None.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Indian plate regex ────────────────────────────────────────────────────────
# State code (2 letters) + district code (2 digits) + series (1–2 letters) +
# registration number (4 digits)
PLATE_REGEX = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{1,2}\d{4}$")

# EasyOCR languages; extend if multi-lingual plates needed
OCR_LANGUAGES: List[str] = ["en"]


# ── Singleton reader ──────────────────────────────────────────────────────────

_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        try:
            import easyocr
        except ImportError as exc:
            raise ImportError(
                "easyocr is not installed. Run: pip install easyocr"
            ) from exc
        logger.info("Initialising EasyOCR reader (languages=%s)…", OCR_LANGUAGES)
        _reader = easyocr.Reader(OCR_LANGUAGES, gpu=False, verbose=False)
        logger.info("EasyOCR reader ready.")
    return _reader


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PlateRead:
    """Result of running OCR on one license-plate crop from one frame."""

    timestamp: float
    raw_text: str            # as returned by EasyOCR, cleaned to upper+alnum
    is_valid_format: bool    # True if raw_text matches PLATE_REGEX
    ocr_confidence: float    # average confidence reported by EasyOCR


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """Uppercase and strip all non-alphanumeric characters."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _crop_bbox(image: np.ndarray, bbox: List[float]) -> Optional[np.ndarray]:
    """
    Crop image to bbox [x1, y1, x2, y2].  Returns None if crop is degenerate.
    """
    x1, y1, x2, y2 = (int(v) for v in bbox)
    h, w = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return image[y1:y2, x1:x2]


# ── Public API ────────────────────────────────────────────────────────────────

def read_plate(
    image: np.ndarray,
    plate_bbox: List[float],
    timestamp: float = 0.0,
) -> Optional[PlateRead]:
    """
    Crop the plate region from *image* and run EasyOCR on it.

    Returns None if the crop is invalid or OCR returns no text.
    """
    crop = _crop_bbox(image, plate_bbox)
    if crop is None:
        logger.debug("Frame %.3fs: degenerate plate crop — skipping.", timestamp)
        return None

    reader = _get_reader()

    try:
        results = reader.readtext(crop, detail=1, paragraph=False)
    except Exception as exc:
        logger.warning("EasyOCR error on frame %.3fs: %s", timestamp, exc)
        return None

    if not results:
        logger.debug("Frame %.3fs: EasyOCR returned no text.", timestamp)
        return None

    # Concatenate all text fragments; OCR may split a plate into segments
    combined_text = "".join(r[1] for r in results)
    avg_conf = sum(r[2] for r in results) / len(results)
    cleaned = _clean(combined_text)

    if not cleaned:
        return None

    is_valid = bool(PLATE_REGEX.match(cleaned))
    logger.debug(
        "Frame %.3fs: raw='%s' cleaned='%s' valid=%s conf=%.2f",
        timestamp, combined_text, cleaned, is_valid, avg_conf,
    )

    return PlateRead(
        timestamp=timestamp,
        raw_text=cleaned,
        is_valid_format=is_valid,
        ocr_confidence=avg_conf,
    )


def majority_vote_plate(reads: List[PlateRead]) -> tuple[Optional[str], float]:
    """
    Given all per-frame PlateReads, determine the best final plate string.

    Algorithm
    ---------
    1. Filter to only format-valid reads.
    2. If none, return (None, 0.0).
    3. Count occurrences of each valid plate string; pick the most common.
    4. OCR agreement ratio = count of the winner / total valid reads.

    Returns
    -------
    (plate_string, ocr_agreement_ratio)
    """
    valid = [r for r in reads if r.is_valid_format]
    if not valid:
        return None, 0.0

    counter: dict[str, int] = {}
    for r in valid:
        counter[r.raw_text] = counter.get(r.raw_text, 0) + 1

    winner = max(counter, key=lambda k: counter[k])
    agreement = counter[winner] / len(valid)

    logger.info(
        "Plate majority vote: '%s' (%d/%d valid reads, agreement=%.2f)",
        winner, counter[winner], len(valid), agreement,
    )
    return winner, agreement
