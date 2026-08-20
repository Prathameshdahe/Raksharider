"""
pipeline/plate_aggregator.py
────────────────────────────
Multi-frame, track-keyed OCR aggregation for Indian license plates.

Inspired by the ANPR-System-main approach (github.com/Tkvmaster/ANPR-System):
  - YOLO finds the plate box (ampr.pt — unchanged)
  - Instead of trusting one frame's OCR read, collect every read across the
    vehicle's tracked lifetime and resolve at clip-end
  - Track ID comes from ByteTrack (already in tracker.py)

Three-tier escalation (cheap → better → VLM):
  Tier 1: EasyOCR       (fast, local, runs on every frame)
  Tier 2: PaddleOCR     (stronger on structured text; runs only when EasyOCR fails regex)
  Tier 3: Gemini vision (VLM plate reader; runs only when both OCR tiers fail)

Key insight from ANPR repo: get_best_ocr() logic — don't trust a single frame;
aggregate across the full track lifetime and take majority/highest-confidence read.

Usage in run_pipeline.py (Stage 5):
    aggregator = PlateAggregator()
    for frame_idx, (fd, frame, tracked_dets) in enumerate(zip(...)):
        plates = fd.by_class("license_plate")
        for plate_det in plates:
            # Find the track_id of the vehicle closest to this plate
            track_id = find_nearest_track_id(plate_det, tracked_dets)
            aggregator.add_raw_read(track_id, frame.image, plate_det.bbox, fd.timestamp)

    results = aggregator.resolve_all()   # one PlateResolution per track
    best = aggregator.best_result(results)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from pipeline.ocr import (
    PlateRead,
    is_valid_indian_plate,
    preprocess_plate_crop,
    _clean_and_disambiguate,
    _get_reader,
    _crop_bbox,
)

logger = logging.getLogger(__name__)

# ── Tier-2: PaddleOCR (optional) ─────────────────────────────────────────────
_paddle_reader = None


def _get_paddle_reader():
    """Lazy-load PaddleOCR. Returns None if not installed."""
    global _paddle_reader
    if _paddle_reader is None:
        try:
            from paddleocr import PaddleOCR
            _paddle_reader = PaddleOCR(lang="en", show_log=False, use_angle_cls=True)
            logger.info("PaddleOCR reader ready.")
        except ImportError:
            logger.info("PaddleOCR not installed — Tier 2 disabled. pip install paddleocr")
            _paddle_reader = False  # sentinel: tried, not available
    return _paddle_reader if _paddle_reader is not False else None


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RawOCRRead:
    """Single OCR read from one frame for one track."""
    timestamp: float
    frame_index: int
    text: str              # already cleaned + disambiguated
    confidence: float
    is_valid: bool         # matches Indian plate regex
    tier: str              # 'easyocr' | 'paddleocr' | 'vlm'


@dataclass
class PlateResolution:
    """Final resolved plate for one tracked vehicle."""
    track_id: int
    plate_text: Optional[str]    # None if never resolved
    confidence: float            # 0.0–1.0
    agreement: float             # fraction of reads that agreed with winner
    total_reads: int
    valid_reads: int
    is_validated: bool           # True if plate_text passes Indian regex
    winning_tier: str            # which tier produced the winner


class PlateAggregator:
    """
    Collects plate OCR reads across frames keyed by ByteTrack track_id.
    Call add_raw_read() once per detected plate per frame.
    Call resolve_all() at clip end to get final results.
    """

    def __init__(self) -> None:
        # track_id -> list of raw reads
        self._reads: Dict[int, List[RawOCRRead]] = defaultdict(list)

    def add_raw_read(
        self,
        track_id: int,
        frame_bgr: np.ndarray,
        plate_bbox: List[float],
        timestamp: float,
        frame_index: int = 0,
    ) -> Optional[RawOCRRead]:
        """
        Run Tier-1 (EasyOCR) on the plate crop and record the result.
        Returns the raw read, or None if crop/OCR failed.
        """
        crop = _crop_bbox(frame_bgr, plate_bbox)
        if crop is None:
            return None

        raw = _easyocr_read(crop, timestamp, frame_index)
        if raw is not None:
            self._reads[track_id].append(raw)
        return raw

    def resolve_track(self, track_id: int) -> PlateResolution:
        """
        Resolve a single track's accumulated reads into one final answer.

        Priority:
          1. Most frequent regex-valid read (majority vote)
          2. Highest-confidence read among valid-format reads
          3. Highest-confidence any read (valid format preferred)
        """
        reads = self._reads.get(track_id, [])
        if not reads:
            return PlateResolution(
                track_id=track_id, plate_text=None, confidence=0.0,
                agreement=0.0, total_reads=0, valid_reads=0,
                is_validated=False, winning_tier="none",
            )

        valid_reads = [r for r in reads if r.is_valid]
        total = len(reads)

        if valid_reads:
            # Majority vote among valid reads
            counter: Dict[str, int] = {}
            tier_map: Dict[str, str] = {}
            conf_map: Dict[str, float] = {}
            for r in valid_reads:
                counter[r.text] = counter.get(r.text, 0) + 1
                if r.confidence > conf_map.get(r.text, 0.0):
                    conf_map[r.text] = r.confidence
                    tier_map[r.text] = r.tier

            winner = max(counter, key=lambda k: (counter[k], conf_map[k]))
            agreement = counter[winner] / len(valid_reads)
            logger.info(
                "Track %d: plate='%s' (%d/%d valid reads, agreement=%.0f%%, tier=%s)",
                track_id, winner, counter[winner], len(valid_reads),
                agreement * 100, tier_map[winner],
            )
            return PlateResolution(
                track_id=track_id,
                plate_text=winner,
                confidence=conf_map[winner],
                agreement=agreement,
                total_reads=total,
                valid_reads=len(valid_reads),
                is_validated=True,
                winning_tier=tier_map[winner],
            )

        # No valid reads — return best confidence read with flag
        best = max(reads, key=lambda r: r.confidence)
        return PlateResolution(
            track_id=track_id,
            plate_text=best.text or None,
            confidence=best.confidence,
            agreement=0.0,
            total_reads=total,
            valid_reads=0,
            is_validated=False,
            winning_tier=best.tier,
        )

    def resolve_all(self) -> Dict[int, PlateResolution]:
        """Resolve all tracks and return dict of track_id → PlateResolution."""
        return {tid: self.resolve_track(tid) for tid in self._reads}

    def best_result(self, resolutions: Dict[int, PlateResolution]) -> Optional[PlateResolution]:
        """Return the single best plate resolution across all tracks."""
        if not resolutions:
            return None
        # Prefer validated plates; among those, highest agreement × confidence
        validated = [r for r in resolutions.values() if r.is_validated and r.plate_text]
        if validated:
            return max(validated, key=lambda r: r.agreement * r.confidence)
        # Fallback: highest confidence among any result
        all_res = [r for r in resolutions.values() if r.plate_text]
        return max(all_res, key=lambda r: r.confidence) if all_res else None

    def escalate_to_paddle(
        self,
        track_id: int,
        frame_bgr: np.ndarray,
        plate_bbox: List[float],
        timestamp: float,
        frame_index: int = 0,
    ) -> Optional[RawOCRRead]:
        """
        Run Tier-2 (PaddleOCR) on a specific crop and add to track reads.
        Call this when EasyOCR failed regex validation on a track.
        """
        paddle = _get_paddle_reader()
        if paddle is None:
            return None

        crop = _crop_bbox(frame_bgr, plate_bbox)
        if crop is None:
            return None

        raw = _paddle_read(paddle, crop, timestamp, frame_index)
        if raw is not None:
            self._reads[track_id].append(raw)
            logger.info(
                "Track %d PaddleOCR: '%s' (conf=%.2f, valid=%s)",
                track_id, raw.text, raw.confidence, raw.is_valid,
            )
        return raw

    def escalate_to_vlm(
        self,
        track_id: int,
        frame_bgr: np.ndarray,
        plate_bbox: List[float],
        timestamp: float,
        frame_index: int = 0,
    ) -> Optional[RawOCRRead]:
        """
        Run Tier-3 (Gemini VLM) on a specific crop and add to track reads.
        Only call when both EasyOCR and PaddleOCR failed.
        """
        crop = _crop_bbox(frame_bgr, plate_bbox)
        if crop is None:
            return None

        raw = _vlm_read(crop, timestamp, frame_index)
        if raw is not None:
            self._reads[track_id].append(raw)
            logger.info(
                "Track %d VLM plate read: '%s' (conf=%.2f, valid=%s)",
                track_id, raw.text, raw.confidence, raw.is_valid,
            )
        return raw

    def has_valid_reads(self, track_id: int) -> bool:
        """True if this track already has at least one regex-valid read."""
        return any(r.is_valid for r in self._reads.get(track_id, []))

    def track_ids(self) -> List[int]:
        return list(self._reads.keys())


# ── OCR tier implementations ──────────────────────────────────────────────────

def _easyocr_read(
    crop: np.ndarray,
    timestamp: float,
    frame_index: int,
) -> Optional[RawOCRRead]:
    """Run EasyOCR (Tier 1) on a pre-cropped plate image."""
    ALLOWED_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    variants = preprocess_plate_crop(crop)
    if not variants:
        return None

    reader = _get_reader()
    best: Optional[RawOCRRead] = None

    for variant in variants:
        try:
            results = reader.readtext(
                variant, detail=1, paragraph=False, allowlist=ALLOWED_CHARS
            )
        except Exception:
            continue
        if not results:
            continue

        combined = "".join(r[1] for r in results)
        avg_conf = sum(r[2] for r in results) / len(results)
        cleaned = _clean_and_disambiguate(combined)
        if not cleaned:
            continue

        valid = is_valid_indian_plate(cleaned)
        candidate = RawOCRRead(
            timestamp=timestamp,
            frame_index=frame_index,
            text=cleaned,
            confidence=avg_conf,
            is_valid=valid,
            tier="easyocr",
        )
        if valid:
            return candidate  # Accept immediately if valid
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    return best


def _paddle_read(
    paddle,
    crop: np.ndarray,
    timestamp: float,
    frame_index: int,
) -> Optional[RawOCRRead]:
    """Run PaddleOCR (Tier 2) on a pre-cropped plate image.

    Adapted from ANPR-System-main filter_text() logic:
    - filter results by bounding-box area proportion
    - join multi-word reads
    """
    variants = preprocess_plate_crop(crop)
    if not variants:
        return None

    for variant in variants:
        try:
            # PaddleOCR expects BGR or RGB numpy array
            if len(variant.shape) == 2:
                variant = cv2.cvtColor(variant, cv2.COLOR_GRAY2BGR)
            result = paddle.ocr(variant, cls=True)
        except Exception as exc:
            logger.debug("PaddleOCR error: %s", exc)
            continue

        if not result or not result[0]:
            continue

        # filter_text logic from ANPR repo: only keep text boxes covering >2% of image
        rect_size = variant.shape[0] * variant.shape[1]
        plate_parts, scores = [], []
        for line in result[0]:
            pts, (text, score) = line[0], line[1]
            length = abs(pts[1][0] - pts[0][0])
            height = abs(pts[2][1] - pts[1][1])
            if length * height / rect_size > 0.02:
                plate_parts.append(text)
                scores.append(score)

        if not plate_parts:
            continue

        import re
        raw_text = re.sub(r"[^A-Z0-9]", "", "".join(plate_parts).upper())
        cleaned = _clean_and_disambiguate(raw_text)
        if not cleaned:
            continue

        conf = max(scores)
        valid = is_valid_indian_plate(cleaned)
        return RawOCRRead(
            timestamp=timestamp,
            frame_index=frame_index,
            text=cleaned,
            confidence=conf,
            is_valid=valid,
            tier="paddleocr",
        )

    return None


def _vlm_read(
    crop: np.ndarray,
    timestamp: float,
    frame_index: int,
) -> Optional[RawOCRRead]:
    """
    Run Gemini Vision (Tier 3) to read the license plate from a crop.
    Reuses the existing Gemini client pattern from vlm.py.
    Only called when both EasyOCR and PaddleOCR produce no valid read.
    """
    try:
        import base64
        import os
        import google.generativeai as genai

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return None

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        _, buf = cv2.imencode(".jpg", crop)
        b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

        prompt = (
            "This is a cropped Indian vehicle license plate image. "
            "Read the plate number exactly as it appears. "
            "Indian plates follow formats like MH01AB1234 or KA05MG2765. "
            "Reply with ONLY the plate number, no spaces, no punctuation, "
            "no explanation. If unreadable, reply with UNREADABLE."
        )

        response = model.generate_content([
            {"mime_type": "image/jpeg", "data": b64},
            prompt,
        ])

        raw = response.text.strip().upper()
        import re
        cleaned = re.sub(r"[^A-Z0-9]", "", raw)
        if not cleaned or cleaned == "UNREADABLE":
            return None

        cleaned = _clean_and_disambiguate(cleaned)
        valid = is_valid_indian_plate(cleaned)
        logger.info("VLM plate read: '%s' valid=%s", cleaned, valid)
        return RawOCRRead(
            timestamp=timestamp,
            frame_index=frame_index,
            text=cleaned,
            confidence=0.85 if valid else 0.5,  # VLM reads are generally reliable
            is_valid=valid,
            tier="vlm",
        )

    except Exception as exc:
        logger.debug("VLM plate read failed: %s", exc)
        return None


# ── Utility: find nearest track_id to a plate detection ─────────────────────

def find_nearest_track_id(
    plate_bbox: List[float],
    tracked_dets: list,  # list[TrackedDetection]
    max_dist_px: float = 300.0,
) -> Optional[int]:
    """
    Match a plate detection to the nearest tracked vehicle by centroid distance.
    Returns None if no tracked detection is within max_dist_px.
    """
    if not tracked_dets:
        return None

    px_c = (plate_bbox[0] + plate_bbox[2]) / 2
    py_c = (plate_bbox[1] + plate_bbox[3]) / 2

    vehicle_classes = {
        "motorcycle", "bicycle", "car", "bus", "truck", "mini_lcv",
        "auto_rickshaw", "vehicle",
    }
    p_w = max(1.0, plate_bbox[2] - plate_bbox[0])
    p_h = max(1.0, plate_bbox[3] - plate_bbox[1])
    adaptive_max_dist = max(max_dist_px, p_w * 6.0, p_h * 8.0)

    best_id: Optional[int] = None
    best_inside_area: Optional[float] = None
    best_dist = adaptive_max_dist

    for td in tracked_dets:
        if not hasattr(td, "bbox") or not hasattr(td, "track_id"):
            continue
        if getattr(td, "track_id", -1) < 0:
            continue
        if getattr(td, "class_name", "") not in vehicle_classes:
            continue

        bx1, by1, bx2, by2 = td.bbox

        # Strong match: plate centroid lies inside a tracked vehicle box.
        if bx1 <= px_c <= bx2 and by1 <= py_c <= by2:
            area = max(1.0, (bx2 - bx1) * (by2 - by1))
            if best_inside_area is None or area < best_inside_area:
                best_inside_area = area
                best_id = td.track_id
            continue

        if best_inside_area is not None:
            continue

        cx = (bx1 + bx2) / 2
        cy = (by1 + by2) / 2
        dist = ((px_c - cx) ** 2 + (py_c - cy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_id = td.track_id

    return best_id
