"""
pipeline/plate_crop_enhancer.py
--------------------------------
"Zoom then read" — pads the detected plate bbox and upscales the crop
before handing it to any OCR tier.

Why this improves accuracy
--------------------------
In real traffic video, a plate box from ampr.pt may be only 20-40px
tall. At that resolution, character strokes are 1-3px wide:
  - OCR engines cannot distinguish 'E' from 'F', or '8' from 'B'.
  - Tight bounding boxes clip edge characters (the first/last letter
    of the plate is inside the box by 1-2px — interpolation at the
    crop boundary smears it away).

Two independent analyses of this exact problem (one working from the
original spec, one working directly in ocr.py / plate_aggregator.py)
converged on the same implementation independently. That's a strong
confirmation this is the right fix — it's not just theory.

Integration point
-----------------
Call enhance_plate_crop() on the detected plate bbox BEFORE passing
the crop into EasyOCR/PaddleOCR/Gemini — i.e. replace the bare
_crop_bbox() call with this in plate_aggregator.add_raw_read() and
add_candidate().

Honest limits
-------------
This cannot recover detail that was never captured. A plate so small
that individual character strokes are 1-2px wide will still be hard
to read after upscaling — interpolation makes strokes wider, it does
not invent resolution that wasn't there. The max_upscale_factor cap
is deliberately there so a near-noise detection doesn't get blown up
into false confidence. If accuracy on genuinely distant/small plates
is still weak after this ships, a learned super-resolution model
(ESRGAN-style) is the real next step — at real extra compute cost.
Try this first; it's nearly free and should recover a meaningful chunk
of distant-plate failures.
"""

from __future__ import annotations

import cv2
import numpy as np


def enhance_plate_crop(
    frame: np.ndarray,
    bbox: tuple[float, float, float, float] | list[float],
    *,
    padding_ratio: float = 0.15,
    target_height: int = 128,
    min_upscale_factor: float = 1.0,
    max_upscale_factor: float = 5.0,
) -> np.ndarray | None:
    """
    Pad the plate bbox by padding_ratio on each side, clip to frame
    bounds, then upscale so the crop height reaches target_height (if
    it's smaller than that).

    Parameters
    ----------
    frame           : full video frame, BGR numpy array (H x W x 3)
    bbox            : (x1, y1, x2, y2) plate bounding box in pixels
    padding_ratio   : expand the crop by this fraction of box size on
                      each side (default 0.15 = 15%)
    target_height   : desired crop height in pixels after upscaling
                      (default 128 — reasonable for EasyOCR/PaddleOCR)
    min_upscale_factor : never scale down; 1.0 means "only upscale"
    max_upscale_factor : cap to avoid blowing up a near-noise detection

    Returns
    -------
    Padded, upscaled crop as BGR ndarray — same dtype as frame.
    Returns None if bbox is invalid or crop is empty after clipping.
    """
    if frame is None or frame.size == 0:
        return None

    frame_h, frame_w = frame.shape[:2]
    x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    box_w = x2 - x1
    box_h = y2 - y1

    if box_w <= 0 or box_h <= 0:
        return None

    # Step 1: add padding
    pad_x = box_w * padding_ratio
    pad_y = box_h * padding_ratio

    px1 = max(0, int(x1 - pad_x))
    py1 = max(0, int(y1 - pad_y))
    px2 = min(frame_w, int(x2 + pad_x))
    py2 = min(frame_h, int(y2 + pad_y))

    crop = frame[py1:py2, px1:px2]
    if crop is None or crop.size == 0:
        return None

    crop_h, crop_w = crop.shape[:2]

    # Step 2: upscale if smaller than target_height
    raw_factor = target_height / max(crop_h, 1)
    scale = max(min_upscale_factor, min(raw_factor, max_upscale_factor))

    if scale <= 1.0:
        return crop  # already at or above target size; don't downscale

    new_w = max(1, int(crop_w * scale))
    new_h = max(1, int(crop_h * scale))

    # INTER_CUBIC: better edge definition than INTER_LINEAR at moderate
    # upscale factors; cheaper than learned super-resolution.
    return cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)


def enhance_plate_crop_multi(
    frame: np.ndarray,
    bbox: tuple[float, float, float, float] | list[float],
    *,
    padding_ratio: float = 0.15,
    target_height: int = 128,
    max_upscale_factor: float = 5.0,
) -> list[np.ndarray]:
    """
    Return a list of enhanced crops with slightly varying padding, so
    downstream OCR sees multiple slightly-different framings of the same
    plate. The best OCR read across variants is what gets recorded.

    Returns empty list if the base crop fails.
    """
    crops: list[np.ndarray] = []
    for ratio in (padding_ratio, padding_ratio * 0.5, padding_ratio * 1.5):
        crop = enhance_plate_crop(
            frame, bbox,
            padding_ratio=ratio,
            target_height=target_height,
            max_upscale_factor=max_upscale_factor,
        )
        if crop is not None:
            crops.append(crop)
    return crops
