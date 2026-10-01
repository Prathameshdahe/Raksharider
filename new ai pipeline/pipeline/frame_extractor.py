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
from typing import Iterator, List, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


@dataclass
class Frame:
    """A single extracted frame with its source timestamp in seconds."""
    timestamp: float    # seconds from video start; 0.0 for images
    image: np.ndarray   # BGR array, shape (H, W, 3)
    index: int = -1     # original frame number in the source video (-1 for images)


@dataclass
class VideoInfo:
    fps: float
    frame_count: int
    duration: float
    width: int
    height: int


def probe_video(source_path: str) -> VideoInfo:
    """Read container metadata without decoding frames."""
    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise ValueError(f"cv2.VideoCapture could not open: {source_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return VideoInfo(fps=fps, frame_count=n, duration=(n / fps if fps > 0 else 0.0), width=w, height=h)


def default_stride(fps: float, target_fps: float = 15.0) -> int:
    """Every frame up to target_fps; every 2nd at 30 fps, every 4th at 60 fps."""
    if fps <= 0:
        return 1
    return max(1, int(round(fps / target_fps)))


def iter_frames(source_path: str, stride: int = 1, max_frames: Optional[int] = None) -> Iterator[Frame]:
    """
    Stream frames sequentially (no seeking), yielding every `stride`-th frame with
    its real timestamp. Memory stays at one frame; the caller keeps what it needs.
    """
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Source not found: {source_path}")
    ext = os.path.splitext(source_path)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        for f in _wrap_image(source_path):
            f.index = 0
            yield f
        return
    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        raise ValueError(f"cv2.VideoCapture could not open: {source_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    idx, yielded = 0, 0
    while True:
        if not cap.grab():
            break
        if idx % stride == 0:
            ok, img = cap.retrieve()
            if not ok:
                break
            ts = (idx / fps) if fps > 0 else cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            yield Frame(timestamp=round(ts, 4), image=img, index=idx)
            yielded += 1
            if max_frames is not None and yielded >= max_frames:
                break
        idx += 1
    cap.release()


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
