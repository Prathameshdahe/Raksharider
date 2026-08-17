"""
pipeline/vlm.py
───────────────
VLM tiebreaker for ambiguous DriveTrust AI cases.

Primary  : Google Gemini (gemini-2.0-flash) — vision model, sends evidence frame as image
Fallback : NVIDIA NIM (nemotron-3.5-lightning-30b-a3b) — text-only reasoning model

Design principles:
  - VLM is NEVER the primary detector — it only confirms or downgrades
  - Fires only on status == "needs_review" to save API cost
  - Returns "auto_flagged", "needs_review", or "insufficient_evidence"
    (same status vocabulary as the rest of the pipeline)
  - Fails gracefully: if all API calls fail, returns the original status
    unchanged and logs a warning

API:
  Gemini   — key from .env → GEMINI_API_KEY  |  model → GEMINI_VLM_MODEL
  NVIDIA   — key from .env → NVIDIA_NIM_API_KEY  |  model → NVIDIA_NIM_REASONING_MODEL

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

# ── Gemini (primary vision model) ─────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_VLM_MODEL = os.environ.get("GEMINI_VLM_MODEL", "gemini-2.0-flash")

# ── NVIDIA NIM (fallback text reasoning model) ────────────────────────────────
NVIDIA_NIM_API_KEY = os.environ.get("NVIDIA_NIM_API_KEY", "")
NVIDIA_NIM_REASONING_MODEL = os.environ.get(
    "NVIDIA_NIM_REASONING_MODEL",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
)
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _load_env_value(key: str) -> str:
    """Load a value from os.environ or the .env file."""
    val = os.environ.get(key, "")
    if val:
        return val
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    return ""


def _frame_to_base64(frame_bgr) -> str:
    """Convert a BGR numpy frame to base64-encoded JPEG."""
    import cv2
    _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def _build_prompt(summary: dict, original_status: str) -> str:
    """Build the structured prompt sent to the VLM."""
    violations  = summary.get("violations_detected", [])
    helmet      = summary.get("helmet_status", "unclear")
    riders      = summary.get("rider_count", 0)
    plate       = summary.get("plate", "unreadable")
    severity    = summary.get("severity_score", 0.0)
    consistency = summary.get("frame_consistency", 0.0)
    vehicle     = summary.get("vehicle_type", "unknown")

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
3. Return ONLY a valid JSON object. No extra text, no markdown fences. Example:

{{"verdict": "needs_review", "reasoning": "Helmet is not clearly visible.", "confidence": 0.6}}

Valid verdict values: auto_flagged, needs_review, insufficient_evidence
Rules:
- auto_flagged: violation is clearly visible and unambiguous
- needs_review: suspicious but not definitive (occlusion, low res, unclear)
- insufficient_evidence: no violation visible in this frame
- You may only confirm or downgrade, never upgrade beyond "{original_status}"
- If the frame is too blurry or dark, return insufficient_evidence"""


def _parse_verdict(raw: str, original_status: str) -> tuple[str, str]:
    """
    Extract and validate verdict JSON from model response.
    Handles markdown fences, leading text, truncated JSON, and preamble robustly.
    """
    import json, re

    # Strip markdown code fences if present (```json ... ```)
    cleaned = raw
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                cleaned = part
                break

    # Find first '{' and decode from there — handles preamble text
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"No JSON found in VLM response: {raw[:300]}")

    parsed = None
    try:
        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(cleaned[start:])
    except json.JSONDecodeError:
        # Truncated JSON fallback — extract fields with regex
        chunk = cleaned[start:]
        verdict_m   = re.search(r'"verdict"\s*:\s*"([^"]+)"', chunk)
        reasoning_m = re.search(r'"reasoning"\s*:\s*"([^"]+)"', chunk)
        if verdict_m:
            parsed = {
                "verdict":   verdict_m.group(1),
                "reasoning": reasoning_m.group(1) if reasoning_m else "Parsed from truncated response.",
            }
        else:
            raise ValueError(f"JSON unparseable even with fallback: {chunk[:200]}")

    verdict   = parsed.get("verdict", original_status)
    reasoning = parsed.get("reasoning", "No reasoning provided.")

    # VLM cannot upgrade beyond original status
    allowed = {
        "auto_flagged":          {"auto_flagged", "needs_review", "insufficient_evidence"},
        "needs_review":          {"needs_review", "insufficient_evidence"},
        "insufficient_evidence": {"insufficient_evidence"},
    }
    if verdict not in allowed.get(original_status, {verdict}):
        logger.warning(
            "VLM tried to upgrade status from %s to %s — ignoring.",
            original_status, verdict,
        )
        verdict = original_status
    return verdict, reasoning


# ── Gemini vision call ────────────────────────────────────────────────────────

def _call_gemini(frame_bgr, summary: dict, original_status: str) -> tuple[str, str]:
    """
    Send evidence frame + structured summary to Gemini vision model.
    Uses the new google-genai SDK (google.genai).
    Returns (verdict, reasoning).
    """
    from google import genai
    from google.genai import types

    api_key    = _load_env_value("GEMINI_API_KEY")
    model_name = _load_env_value("GEMINI_VLM_MODEL") or GEMINI_VLM_MODEL

    if not api_key or api_key == "your-gemini-api-key-here":
        raise ValueError("GEMINI_API_KEY not configured in .env")

    client = genai.Client(api_key=api_key)

    # Encode BGR frame to JPEG bytes
    import cv2
    _, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    image_bytes = buf.tobytes()

    prompt = _build_prompt(summary, original_status)

    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=1024,   # increased — model needs room for full JSON
        ),
    )

    raw = response.text.strip()
    logger.debug("Gemini VLM raw response: %s", raw)
    return _parse_verdict(raw, original_status)


# ── NVIDIA NIM text reasoning fallback ───────────────────────────────────────

def _call_nvidia_reasoning(summary: dict, original_status: str) -> tuple[str, str]:
    """
    Text-only fallback using NVIDIA nemotron reasoning model.
    No image sent — uses only the structured detection summary.
    """
    from openai import OpenAI

    api_key = _load_env_value("NVIDIA_NIM_API_KEY")
    if not api_key:
        raise ValueError("NVIDIA_NIM_API_KEY not configured in .env")

    client = OpenAI(base_url=NVIDIA_NIM_BASE_URL, api_key=api_key)
    prompt = _build_prompt(summary, original_status)

    resp = client.chat.completions.create(
        model=NVIDIA_NIM_REASONING_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        top_p=0.95,
        max_tokens=1024,
        extra_body={
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_budget": 1024,
        },
    )
    raw = resp.choices[0].message.content.strip()
    logger.debug("NVIDIA reasoning VLM raw response: %s", raw)
    return _parse_verdict(raw, original_status)


# ── Public API ────────────────────────────────────────────────────────────────

def vlm_tiebreaker(
    evidence_frame_bgr,         # np.ndarray (BGR)
    structured_summary: dict,
    original_status: str = "needs_review",
) -> tuple[str, str]:
    """
    Run the VLM tiebreaker on one evidence frame.

    Flow:
        1. Try Gemini vision model (image + text prompt)
        2. If Gemini unavailable/fails → fallback to NVIDIA text reasoning model
        3. If both fail → return original_status unchanged

    Returns:
        (new_status, reasoning_text)
        new_status is one of: auto_flagged / needs_review / insufficient_evidence
    """
    if original_status != "needs_review":
        return original_status, "VLM not needed for this status."

    # ── 1. Try Gemini (vision) ─────────────────────────────────────────────────
    try:
        verdict, reasoning = _call_gemini(evidence_frame_bgr, structured_summary, original_status)
        logger.info("Gemini VLM tiebreaker: %s → %s | %s", original_status, verdict, reasoning)
        return verdict, reasoning
    except Exception as gemini_exc:
        exc_msg = str(gemini_exc)
        if "429" in exc_msg or "RESOURCE_EXHAUSTED" in exc_msg:
            logger.warning("Gemini credits exhausted (429) — falling back to NVIDIA reasoning model.")
        elif "GEMINI_API_KEY not configured" in exc_msg:
            logger.warning("Gemini not configured — falling back to NVIDIA reasoning model.")
        else:
            logger.warning("Gemini VLM failed (%s) — falling back to NVIDIA reasoning model.", gemini_exc)

    # ── 2. Fallback: NVIDIA text reasoning ────────────────────────────────────
    try:
        verdict, reasoning = _call_nvidia_reasoning(structured_summary, original_status)
        logger.info(
            "NVIDIA reasoning fallback: %s → %s | %s", original_status, verdict, reasoning
        )
        return verdict, reasoning
    except Exception as nvidia_exc:
        logger.warning("NVIDIA reasoning fallback also failed (%s).", nvidia_exc)

    # ── 3. Both failed — keep original ────────────────────────────────────────
    return original_status, "VLM unavailable — both Gemini and NVIDIA failed."


def check_vlm_available() -> bool:
    """Returns True if at least one VLM backend is configured."""
    gemini_key = _load_env_value("GEMINI_API_KEY")
    nvidia_key  = _load_env_value("NVIDIA_NIM_API_KEY")
    gemini_ok   = bool(gemini_key and gemini_key != "your-gemini-api-key-here")
    try:
        from openai import OpenAI  # noqa: F401
        nvidia_ok = bool(nvidia_key)
    except ImportError:
        nvidia_ok = False
    try:
        from google import genai  # noqa: F401
    except ImportError:
        gemini_ok = False
    return gemini_ok or nvidia_ok

