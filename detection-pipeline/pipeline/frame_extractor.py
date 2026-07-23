"""
pipeline/frame_extractor.py
───────────────────────────
Extracts frames from a video file at a configurable sampling interval, or
wraps a single image as a one-element list so every downstream module can
assume it always receives a list of (timestamp, np.ndarray) tuples.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Recognised image extensions — anything else is treated as video
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


@dataclass
class Frame:
    """A single extracted frame with its source timestamp (seconds)."""

    timestamp: float          # seconds from start of video (0.0 for images)
    image: np.ndarray         # BGR numpy array, shape (H, W, 3)


def extract_frames(
    source_path: str,
    sample_interval: float = 0.5,
) -> List[Frame]:
    """
    Extract frames from *source_path*.

    Parameters
    ----------
    source_path:
        Absolute or relative path to a video file or image file.
    sample_interval:
        For video: how many seconds between sampled frames (default 0.5 s).
        Ignored for image inputs.

    Returns
    -------
    List[Frame]
        Non-empty list of Frame objects.  Raises ValueError if the file
        cannot be opened or no frames are extracted.
    """
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Source not found: {source_path}")

    ext = os.path.splitext(source_path)[1].lower()

    if ext in _IMAGE_EXTENSIONS:
        return _wrap_image(source_path)
    else:
        return _sample_video(source_path, sample_interval)


# ── Private helpers ──────────────────────────────────────────────────────────

def _wrap_image(path: str) -> List[Frame]:
    """Load a single image and return it as a one-element list."""
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"cv2.imread could not open image: {path}")
    logger.info("Image input detected — wrapping as single-frame list: %s", path)
    return [Frame(timestamp=0.0, image=img)]


def _sample_video(path: str, interval: float) -> List[Frame]:
    """
    Sample a video at *interval* seconds between frames.

    Strategy
    --------
    1. Seek to each target timestamp using CAP_PROP_POS_MSEC for accuracy.
    2. Fall back to sequential read if seeking is unreliable (older codecs).
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"cv2.VideoCapture could not open video: {path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0

    logger.info(
        "Video opened: %s | FPS=%.2f | frames=%d | duration=%.2fs | interval=%.2fs",
        path, fps, total_frames, duration, interval,
    )

    frames: List[Frame] = []
    target_ts = 0.0

    while True:
        cap.set(cv2.CAP_PROP_POS_MSEC, target_ts * 1000.0)
        ret, img = cap.read()
        if not ret:
            break

        actual_ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        frames.append(Frame(timestamp=actual_ts, image=img))
        logger.debug("  Sampled frame at %.3fs", actual_ts)

        target_ts += interval

    cap.release()

    if not frames:
        raise ValueError(f"No frames could be extracted from: {path}")

    logger.info("Extracted %d frames from video.", len(frames))
    return frames
