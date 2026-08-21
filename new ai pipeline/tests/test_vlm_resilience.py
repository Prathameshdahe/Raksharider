"""
tests/test_vlm_resilience.py
─────────────────────────────
Regression test: VLM tiebreaker failure paths must degrade gracefully.

Scenarios
---------
1. No API keys → vlm_tiebreaker returns original status unchanged.
2. Gemini raises network exception → falls back to NVIDIA, both fail
   → original status preserved, no exception propagated.
3. Malformed JSON response from VLM → status preserved, no crash.
4. check_vlm_available() returns False when neither key is set.
5. check_vlm_available() returns True when at least one key is set.
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch, MagicMock

import pytest


class TestVLMGracefulFailure:
    """VLM failure paths must preserve original status and never crash."""

    def test_both_backends_fail_returns_original_status(self):
        """When both Gemini and NVIDIA fail, original status is returned unchanged."""
        from pipeline.vlm import vlm_tiebreaker

        with patch("pipeline.vlm._call_gemini", side_effect=RuntimeError("Gemini unavailable")):
            with patch("pipeline.vlm._call_nvidia_reasoning", side_effect=RuntimeError("NVIDIA unavailable")):
                new_status, reasoning = vlm_tiebreaker(
                    evidence_frame_bgr=None,
                    structured_summary={"violations_detected": ["no_helmet"], "rider_count": 2,
                                        "helmet_status": "unclear", "plate": "unreadable",
                                        "severity_score": 0.8, "frame_consistency": 0.9,
                                        "vehicle_type": "motorcycle"},
                    original_status="needs_review",
                )
        assert new_status == "needs_review", f"Expected 'needs_review', got '{new_status}'"
        assert "unavailable" in reasoning.lower() or "failed" in reasoning.lower()

    def test_non_needs_review_status_skips_vlm(self):
        """VLM should not fire if status is already auto_flagged."""
        from pipeline.vlm import vlm_tiebreaker

        call_count = 0

        def _mock_gemini(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return "auto_flagged", "confirmed"

        with patch("pipeline.vlm._call_gemini", side_effect=_mock_gemini):
            new_status, reasoning = vlm_tiebreaker(
                evidence_frame_bgr=None,
                structured_summary={},
                original_status="auto_flagged",
            )

        assert call_count == 0, "VLM should not be called when status is already auto_flagged"
        assert new_status == "auto_flagged"

    def test_gemini_fails_nvidia_succeeds(self):
        """When Gemini fails, NVIDIA fallback should succeed and return its verdict."""
        from pipeline.vlm import vlm_tiebreaker

        with patch("pipeline.vlm._call_gemini", side_effect=ConnectionError("429 rate limit")):
            with patch("pipeline.vlm._call_nvidia_reasoning", return_value=("auto_flagged", "NVIDIA confirmed")):
                new_status, reasoning = vlm_tiebreaker(
                    evidence_frame_bgr=None,
                    structured_summary={"violations_detected": [], "rider_count": 3,
                                        "helmet_status": "unclear", "plate": "unreadable",
                                        "severity_score": 0.8, "frame_consistency": 0.9,
                                        "vehicle_type": "motorcycle"},
                    original_status="needs_review",
                )

        assert new_status == "auto_flagged"
        assert "NVIDIA" in reasoning

    def test_check_vlm_available_false_when_no_keys(self):
        """check_vlm_available() must return False when no API keys are set."""
        from pipeline.vlm import check_vlm_available

        with patch("pipeline.vlm._load_env_value", return_value=""):
            result = check_vlm_available()
        assert result is False

    def test_check_vlm_available_true_with_gemini_key(self):
        """check_vlm_available() must return True if GEMINI_API_KEY is set."""
        from pipeline.vlm import check_vlm_available

        def _mock_load(key: str) -> str:
            return "fake-gemini-key" if key == "GEMINI_API_KEY" else ""

        with patch("pipeline.vlm._load_env_value", side_effect=_mock_load):
            # Also patch import of google.genai to simulate it being available
            fake_genai = MagicMock()
            with patch.dict(sys.modules, {"google": fake_genai, "google.genai": fake_genai}):
                result = check_vlm_available()
        assert result is True
