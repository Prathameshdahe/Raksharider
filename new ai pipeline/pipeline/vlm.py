"""
pipeline/vlm.py
───────────────
VLM tiebreaker for ambiguous DriveTrust AI cases.

Uses NVIDIA NIM API (Nemotron model) to validate a `needs_review` verdict
by examining the evidence frame and the structured detection summary.

Design principles:
  - VLM is NEVER the primary detector — it only confirms or downgrades
  - Fires only on status == "needs_review" to save API cost
  - Returns "auto_flagged", "needs_review", or "insufficient_evidence"
    (same status vocabulary as the rest of the pipeline)
  - Fails gracefully: if the API call fails, returns the original status
    unchanged and logs a warning

API:
  NVIDIA NIM — model: nvidia/nemotron-4-340b-instruct (or similar available)
  base_url:    https://integrate.api.nvidia.com/v1
  Key loaded from: .env → NVIDIA_NIM_API_KEY

Usage:
  from pipeline.vlm import vlm_tiebreaker
  new_status, reasoning = vlm_tiebreaker(
      evidence_frame_bgr=frame,
      structured_summary=summary_dict,
      original_status="needs_review",
  )
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model selection — fall back through available NIM models ──────────────────
# Check available models: GET https://integrate.api.nvidia.com/v1/models
NVIDIA_NIM_MODEL = os.environ.get(
    "NVIDIA_NIM_MODEL",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",   # best available in NIM
)
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _load_api_key() -> Optional[str]:
    """Load NVIDIA NIM API key from environment or .env file."""
    key = os.environ.get("NVIDIA_NIM_API_KEY")
    if key:
        return key
    # Try loading from .env file
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("NVIDIA_NIM_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return None


def _frame_to_base64(frame_bgr) -> str:
    """Convert a BGR numpy frame to base64-encoded JPEG for the API."""
    import cv2
    _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _build_prompt(summary: dict, original_status: str) -> str:
    """Build the structured prompt sent to the VLM."""
    violations = summary.get("violations_detected", [])
    helmet     = summary.get("helmet_status", "unclear")
    riders     = summary.get("rider_count", 0)
    plate      = summary.get("plate", "unreadable")
    severity   = summary.get("severity_score", 0.0)
    consistency= summary.get("frame_consistency", 0.0)
    vehicle    = summary.get("vehicle_type", "unknown")

    return f"""You are a road-safety compliance reviewer for DriveTrust AI (India).
You are reviewing a single evidence frame from a traffic camera clip.

The automated pipeline has flagged this clip as "{original_status}" with the following detections:
- Vehicle type: {vehicle}
- Violations detected: {violations if violations else "none"}
- Helmet status: {helmet}
- Rider count: {riders}
- Number plate: {plate}
- Severity score: {severity:.2f} (0=none, 1=definite)
- Frame consistency: {consistency:.2f}

Your job:
1. Look at the evidence frame carefully.
2. Determine whether the automated detections look correct.
3. Return ONLY a JSON object in this exact format, no other text:

{{
  "verdict": "auto_flagged" | "needs_review" | "insufficient_evidence",
  "reasoning": "One sentence explaining your decision.",
  "confidence": 0.0
}}

Rules:
- "auto_flagged": violation is clearly visible and unambiguous in the frame
- "needs_review": something suspicious but not definitive (occlusion, low res, unclear)
- "insufficient_evidence": no violation visible in this frame
- You may only confirm or downgrade — never upgrade beyond the original status
- If the frame is too blurry or dark to judge, return "insufficient_evidence"
- Do NOT guess or fabricate — if you cannot see it clearly, say insufficient_evidence"""


def vlm_tiebreaker(
    evidence_frame_bgr,       # np.ndarray (BGR)
    structured_summary: dict,
    original_status: str = "needs_review",
) -> tuple[str, str]:
    """
    Run the VLM tiebreaker on one evidence frame.

    Returns:
        (new_status, reasoning_text)
        new_status is one of: auto_flagged / needs_review / insufficient_evidence
        On any error, returns (original_status, "VLM unavailable") safely.
    """
    if original_status != "needs_review":
        # Only fire on ambiguous cases — not on definitive ones
        return original_status, "VLM not needed for this status."

    api_key = _load_api_key()
    if not api_key:
        logger.warning("NVIDIA_NIM_API_KEY not found — VLM tiebreaker skipped.")
        return original_status, "VLM skipped: API key not configured."

    try:
        from openai import OpenAI
        client = OpenAI(base_url=NVIDIA_NIM_BASE_URL, api_key=api_key)

        img_b64  = _frame_to_base64(evidence_frame_bgr)
        prompt   = _build_prompt(structured_summary, original_status)

        response = client.chat.completions.create(
            model=NVIDIA_NIM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}",
                            },
                        },
                    ],
                }
            ],
            temperature=0.1,    # low temp for consistent structured output
            max_tokens=512,
        )

        raw = response.choices[0].message.content.strip()
        logger.debug("VLM raw response: %s", raw)

        # Parse JSON response
        import json, re
        # Extract JSON block if surrounded by markdown fences
        json_match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON found in VLM response: {raw}")

        parsed    = json.loads(json_match.group())
        verdict   = parsed.get("verdict", original_status)
        reasoning = parsed.get("reasoning", "No reasoning provided.")

        # Validate — VLM cannot upgrade beyond original
        allowed = {
            "auto_flagged":          {"auto_flagged", "needs_review", "insufficient_evidence"},
            "needs_review":          {"needs_review", "insufficient_evidence"},
            "insufficient_evidence": {"insufficient_evidence"},
        }
        if verdict not in allowed.get(original_status, {verdict}):
            logger.warning(
                "VLM tried to upgrade status from %s to %s — ignoring, keeping original.",
                original_status, verdict,
            )
            verdict = original_status

        logger.info(
            "VLM tiebreaker: %s → %s | %s",
            original_status, verdict, reasoning,
        )
        return verdict, reasoning

    except Exception as exc:
        logger.warning("VLM tiebreaker failed (%s) — keeping original status.", exc)
        return original_status, f"VLM error: {exc}"


def check_vlm_available() -> bool:
    """Quick check — returns True if VLM can be called (key present + openai importable)."""
    try:
        from openai import OpenAI  # noqa: F401
        return _load_api_key() is not None
    except ImportError:
        return False
