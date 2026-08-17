"""
pipeline/ocr.py
---------------
Advanced OCR pipeline for Indian license plates, incorporating image
preprocessing techniques (CLAHE, bilateral filtering, Otsu binarization,
border clearing, and aspect-ratio normalization).

Plate formats supported:
  - Standard Indian RTO: ^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$ (e.g. MH02FX9484, DL1C1234, KA05MG2765)
  - Bharat Series (BH):  ^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$ (e.g. 22BH1234AA)

Design:
  1. Multi-stage image preprocessing (upscale -> CLAHE -> bilateral filter -> binarize/clean)
  2. Character allowlist (A-Z, 0-9) to suppress noise symbols
  3. Strip high-security "IND" watermark prefix
  4. OCR character disambiguation (O/0, I/1, B/8, S/5, Z/2) based on Indian plate syntax
  5. Multi-frame majority voting across clip
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Standard Indian License Plate Patterns:
# Standard RTO: State (2 letters) + District (1-2 digits) + Series (0-3 letters) + Number (4 digits)
STANDARD_RTO_REGEX = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$")
# Bharat Series: Year (2 digits) + BH + Number (4 digits) + Series (1-2 letters)
BHARAT_SERIES_REGEX = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")

OCR_LANGUAGES: List[str] = ["en"]
ALLOWED_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

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
    is_valid_format: bool   # True if matches Indian plate regex
    ocr_confidence: float   # mean confidence from EasyOCR


def preprocess_plate_crop(crop: np.ndarray) -> List[np.ndarray]:
    """
    Generate enhanced variants of the license plate crop to maximize OCR readability:
      Variant 1: High-contrast CLAHE grayscale (sharp edges, normalized lighting)
      Variant 2: Morphological cleaned & Otsu binarized with cleared borders
      Variant 3: Original upscaled BGR
    """
    h, w = crop.shape[:2]
    if h == 0 or w == 0:
        return []

    # 1. Upscale if plate is small (target height ~80-120px)
    target_h = max(90, h)
    scale = target_h / h
    target_w = int(w * scale)
    resized = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_CUBIC)

    # 2. Grayscale conversion
    if len(resized.shape) == 3:
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    else:
        gray = resized.copy()

    # 3. CLAHE (Contrast Limited Adaptive Histogram Equalization)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced_gray = clahe.apply(gray)

    # 4. Bilateral filtering (smooths background noise while preserving sharp letter edges)
    denoised = cv2.bilateralFilter(enhanced_gray, d=9, sigmaColor=75, sigmaSpace=75)

    # 5. Otsu thresholding + border clearing (technique from Indian license plate repo)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    # Border clearing: remove 3px margin noise around the plate edge
    border_px = 3
    binary[:border_px, :] = 255
    binary[-border_px:, :] = 255
    binary[:, :border_px] = 255
    binary[:, -border_px:] = 255

    # Invert binary to black-on-white text if needed
    white_pixels = cv2.countNonZero(binary)
    total_pixels = binary.shape[0] * binary.shape[1]
    if white_pixels < total_pixels / 2:
        binary = cv2.bitwise_not(binary)

    # Return multiple candidate variants for OCR evaluation
    return [denoised, binary, resized]


def _clean_and_disambiguate(text: str) -> str:
    """
    Clean OCR text, strip 'IND' HSRP watermark, and apply positional character disambiguation.
    """
    cleaned = re.sub(r"[^A-Z0-9]", "", text.upper())

    # Strip HSRP watermark 'IND' prefix if present
    if cleaned.startswith("IND") and len(cleaned) > 7:
        cleaned = cleaned[3:]

    # Positional character correction for standard Indian plates (e.g. XX 00 XX 0000)
    # State code (first 2 chars) must be letters:
    chars = list(cleaned)
    if len(chars) >= 2:
        for i in (0, 1):
            if chars[i] == '0': chars[i] = 'O'
            elif chars[i] == '1': chars[i] = 'I'
            elif chars[i] == '5': chars[i] = 'S'
            elif chars[i] == '8': chars[i] = 'B'

    # District code (chars 2, 3) must be digits:
    if len(chars) >= 4 and not chars[2].isalpha():
        for i in (2, 3):
            if i < len(chars):
                if chars[i] == 'O' or chars[i] == 'D' or chars[i] == 'Q': chars[i] = '0'
                elif chars[i] == 'I' or chars[i] == 'L': chars[i] = '1'
                elif chars[i] == 'Z': chars[i] = '2'
                elif chars[i] == 'S': chars[i] = '5'
                elif chars[i] == 'B': chars[i] = '8'

    # Last 4 digits (chars -4 to end) must be digits:
    if len(chars) >= 8:
        for i in range(len(chars) - 4, len(chars)):
            if chars[i] == 'O' or chars[i] == 'D' or chars[i] == 'Q': chars[i] = '0'
            elif chars[i] == 'I' or chars[i] == 'L': chars[i] = '1'
            elif chars[i] == 'Z': chars[i] = '2'
            elif chars[i] == 'S': chars[i] = '5'
            elif chars[i] == 'B': chars[i] = '8'

    return "".join(chars)


def is_valid_indian_plate(text: str) -> bool:
    """Check if a string matches standard Indian RTO or Bharat series format."""
    return bool(STANDARD_RTO_REGEX.match(text) or BHARAT_SERIES_REGEX.match(text))


def _crop_bbox(image: np.ndarray, bbox: List[float]) -> Optional[np.ndarray]:
    """Crop [x1,y1,x2,y2] from image. Returns None if invalid."""
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
    Crop the plate region, apply multi-variant enhancement, and run EasyOCR with allowlist.
    """
    crop = _crop_bbox(image, plate_bbox)
    if crop is None:
        return None

    variants = preprocess_plate_crop(crop)
    if not variants:
        return None

    reader = _get_reader()
    best_read: Optional[PlateRead] = None

    for variant in variants:
        try:
            results = reader.readtext(
                variant,
                detail=1,
                paragraph=False,
                allowlist=ALLOWED_CHARS,
            )
        except Exception as exc:
            logger.debug("EasyOCR variant read failed at t=%.3fs: %s", timestamp, exc)
            continue

        if not results:
            continue

        combined = "".join(r[1] for r in results)
        avg_conf = sum(r[2] for r in results) / len(results)
        cleaned = _clean_and_disambiguate(combined)

        if not cleaned:
            continue

        valid = is_valid_indian_plate(cleaned)
        candidate = PlateRead(
            timestamp=timestamp,
            raw_text=cleaned,
            is_valid_format=valid,
            ocr_confidence=avg_conf,
        )

        # If we got a valid Indian format plate, accept it immediately
        if valid:
            return candidate

        # Otherwise keep the highest confidence read
        if best_read is None or candidate.ocr_confidence > best_read.ocr_confidence:
            best_read = candidate

    return best_read


def majority_vote_plate(reads: List[PlateRead]) -> tuple[Optional[str], float]:
    """
    Return the most common format-valid plate string across all frames,
    and the fraction of valid reads that agree with it.
    """
    valid = [r for r in reads if r.is_valid_format]
    if not valid:
        # Fallback to most frequent high-confidence non-empty read if no exact regex match
        non_empty = [r for r in reads if len(r.raw_text) >= 6]
        if not non_empty:
            return None, 0.0
        counter: dict[str, int] = {}
        for r in non_empty:
            counter[r.raw_text] = counter.get(r.raw_text, 0) + 1
        winner = max(counter, key=lambda k: counter[k])
        return winner, counter[winner] / len(non_empty)

    counter: dict[str, int] = {}
    for r in valid:
        counter[r.raw_text] = counter.get(r.raw_text, 0) + 1

    winner = max(counter, key=lambda k: counter[k])
    agreement = counter[winner] / len(valid)
    logger.info("Plate: '%s' (%d/%d valid reads, agreement=%.2f)", winner, counter[winner], len(valid), agreement)
    return winner, agreement
