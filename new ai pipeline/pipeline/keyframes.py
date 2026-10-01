"""
pipeline/keyframes.py
---------------------
Per-track keyframe store for streaming runs.

Every frame is decoded once and thrown away; a track keeps only its K best
frames (largest box × sharpest crop, spread in time) as JPEG bytes. Those are
the frames the expensive models already looked at and the frames that become
evidence. Memory is bounded by tracks × K, not by video length.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np


@dataclass
class Keyframe:
    frame_index: int
    timestamp: float
    score: float
    bbox: List[float]
    jpeg: bytes

    def decode(self) -> np.ndarray:
        return cv2.imdecode(np.frombuffer(self.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)


def sharpness(img: np.ndarray) -> float:
    """Variance of the Laplacian on a small grey version, squashed to 0..1."""
    if img is None or img.size == 0:
        return 0.0
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = g.shape[:2]
    if max(h, w) > 160:
        s = 160.0 / max(h, w)
        g = cv2.resize(g, (max(1, int(w * s)), max(1, int(h * s))))
    v = float(cv2.Laplacian(g, cv2.CV_64F).var())
    return min(1.0, v / 800.0)


def frame_score(bbox: List[float], frame_shape: tuple, crop: Optional[np.ndarray]) -> float:
    h, w = frame_shape[:2]
    area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]) / float(max(1, w * h))
    return (area ** 0.5) * (0.5 + 0.5 * sharpness(crop))


class KeyframeStore:
    def __init__(self, per_track: int = 5, min_gap_s: float = 0.4, max_width: int = 1920, jpeg_quality: int = 85) -> None:
        self.per_track, self.min_gap_s, self.max_width, self.q = per_track, min_gap_s, max_width, jpeg_quality
        self._kf: Dict[int, List[Keyframe]] = {}

    # ── encoding (once per frame, shared by every track that keeps it) ───────
    def encoder(self, frame: np.ndarray) -> Callable[[], bytes]:
        cache: dict = {}
        def enc() -> bytes:
            if "b" not in cache:
                img = frame
                h, w = img.shape[:2]
                if w > self.max_width:
                    s = self.max_width / w
                    img = cv2.resize(img, (self.max_width, int(h * s)), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.q])
                cache["b"] = buf.tobytes() if ok else b""
            return cache["b"]
        return enc

    def scale(self, frame_shape: tuple) -> float:
        w = frame_shape[1]
        return self.max_width / w if w > self.max_width else 1.0

    def consider(self, track_id: int, frame_index: int, timestamp: float, bbox: List[float],
                 score: float, encode: Callable[[], bytes]) -> bool:
        kfs = self._kf.setdefault(track_id, [])
        # temporal spread: replace a near-in-time keyframe only if this one is better
        for i, k in enumerate(kfs):
            if abs(k.timestamp - timestamp) < self.min_gap_s:
                if score > k.score:
                    kfs[i] = Keyframe(frame_index, timestamp, score, list(bbox), encode())
                    return True
                return False
        if len(kfs) < self.per_track:
            kfs.append(Keyframe(frame_index, timestamp, score, list(bbox), encode()))
            return True
        worst = min(range(len(kfs)), key=lambda i: kfs[i].score)
        if score > kfs[worst].score:
            kfs[worst] = Keyframe(frame_index, timestamp, score, list(bbox), encode())
            return True
        return False

    def frames(self, track_id: int) -> List[Keyframe]:
        return sorted(self._kf.get(track_id, []), key=lambda k: k.score, reverse=True)

    def by_index(self, track_id: int, frame_index: int) -> Optional[Keyframe]:
        for k in self._kf.get(track_id, []):
            if k.frame_index == frame_index:
                return k
        return None

    def merge(self, primary: int, fragment: int) -> None:
        if fragment == primary or fragment not in self._kf:
            return
        for k in self._kf.pop(fragment):
            self.consider(primary, k.frame_index, k.timestamp, k.bbox, k.score, lambda b=k.jpeg: b)

    def drop(self, track_id: int) -> None:
        self._kf.pop(track_id, None)

    def track_ids(self) -> List[int]:
        return list(self._kf)

    def __len__(self) -> int:
        return sum(len(v) for v in self._kf.values())
