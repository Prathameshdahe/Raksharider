"""
pipeline/frame_extractor.py
---------------------------
Samples frames from a video at a configurable interval, or wraps a single
image as a one-element list so downstream modules always work with a list
of Frame objects.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


@dataclass
class Frame:
    """A single extracted frame with its source timestamp in seconds."""
    timestamp: float    # seconds from video start; 0.0 for images
    image: np.ndarray   # BGR array, shape (H, W, 3)


def extract_frames(source_path: str, sample_interval: float = 0.5) -> List[Frame]:
    """
    Extract frames from a video or image file.

    For video: samples one frame every sample_interval seconds.
    For image: returns a single Frame with timestamp 0.0.
    Raises FileNotFoundError if the path does not exist,
    ValueError if the file cannot be opened or yields no frames.
    """
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Source not found: {source_path}")

    ext = os.path.splitext(source_path)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return _wrap_image(source_path)
    return _sample_video(source_path, sample_interval)


def _wrap_image(path: str) -> List[Frame]:
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"cv2.imread could not open: {path}")
    logger.info("Image input: %s", path)
    return [Frame(timestamp=0.0, image=img)]


def _sample_video(path: str, interval: float) -> List[Frame]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"cv2.VideoCapture could not open: {path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0
    logger.info(
        "Video: %s | FPS=%.2f | frames=%d | duration=%.2fs | interval=%.2fs",
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
        target_ts += interval

    cap.release()

    if not frames:
        raise ValueError(f"No frames extracted from: {path}")

    logger.info("Extracted %d frames from video.", len(frames))
    return frames
