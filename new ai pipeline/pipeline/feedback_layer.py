"""
pipeline/feedback_layer.py
---------------------------
Manual human review layer: logs human agree/disagree/uncertain judgments
against AI verdicts, surfaces disagreement-rate and confidence-calibration
patterns.

Design principles (from master build spec Section 6)
-----------------------------------------------------
1. This layer does NOT auto-adjust anything. A human decides what to act
   on. An AI system retraining on its own uncorrected output is a real
   accuracy-drift risk for a system with real consequences (trust scores).

2. The log is append-only. Past judgments are never modified — only new
   ones are appended. Editing the log requires deleting it manually.

3. The calibration report surfaces patterns, not corrections. For example:
   "triple_riding calls at confidence > 0.8 have 42% human disagreement"
   is a useful signal that the model is overconfident — but acting on it
   (e.g. raising the confirmation threshold) requires a human decision.

Real case this caught during testing
-------------------------------------
High-confidence triple-riding calls were LESS accurate than low-confidence
ones — the opposite of expected. Root cause: pedestrians walking close to
a parked motorcycle at an intersection were classified as riders. The
pedestrian-adjacency pattern is invisible to a single-frame confidence
score but visible once human disagreement is aggregated by confidence band.

Usage
-----
    from pipeline.feedback_layer import FeedbackLayer

    fl = FeedbackLayer("feedback_log.jsonl")

    # After admin reviews a case:
    fl.log_judgment(
        run_id="abc123",
        track_id=7,
        violation="triple_riding",
        ai_result="confirmed",
        ai_confidence=0.87,
        human_judgment="disagree",   # 'agree' | 'disagree' | 'uncertain'
        reviewer_note="Pedestrians on footpath, not riders.",
    )

    # Periodic calibration report (e.g. admin dashboard):
    report = fl.calibration_report()
    print(report)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_JUDGMENTS = frozenset({"agree", "disagree", "uncertain"})


@dataclass
class FeedbackEntry:
    """One human judgment on one AI verdict."""
    timestamp: str               # ISO 8601 UTC
    run_id: str
    track_id: int
    violation: str
    ai_result: str               # 'confirmed' | 'insufficient_evidence' | 'not_present'
    ai_confidence: float         # 0.0–1.0
    human_judgment: str          # 'agree' | 'disagree' | 'uncertain'
    reviewer_note: str = ""


@dataclass
class CalibrationBucket:
    """Aggregated feedback stats for one (violation, confidence_band) pair."""
    violation: str
    confidence_band: str         # e.g. '0.7-0.8'
    total: int
    agree: int
    disagree: int
    uncertain: int
    disagreement_rate: float     # disagree / total
    note: str = ""               # auto-filled if disagreement_rate is high


class FeedbackLayer:
    """
    Append-only feedback log with calibration reporting.

    log_path: path to a .jsonl file (JSON Lines — one entry per line).
              Created if it doesn't exist.
    """

    HIGH_DISAGREEMENT_THRESHOLD: float = 0.30   # flag if > 30% human disagreement

    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file if it doesn't exist
        if not self.log_path.exists():
            self.log_path.write_text("")

    def log_judgment(
        self,
        *,
        run_id: str,
        track_id: int,
        violation: str,
        ai_result: str,
        ai_confidence: float,
        human_judgment: str,
        reviewer_note: str = "",
    ) -> None:
        """
        Append one human judgment to the log.

        human_judgment must be 'agree', 'disagree', or 'uncertain'.
        """
        if human_judgment not in VALID_JUDGMENTS:
            raise ValueError(
                f"human_judgment must be one of {VALID_JUDGMENTS}, got '{human_judgment}'"
            )

        entry = FeedbackEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            track_id=track_id,
            violation=violation,
            ai_result=ai_result,
            ai_confidence=round(float(ai_confidence), 4),
            human_judgment=human_judgment,
            reviewer_note=reviewer_note,
        )

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

        logger.info(
            "Feedback logged: run=%s track=%d %s → %s (human: %s)",
            run_id, track_id, violation, ai_result, human_judgment,
        )

    def load_entries(self) -> List[FeedbackEntry]:
        """Load all feedback entries from the log file."""
        entries: List[FeedbackEntry] = []
        if not self.log_path.exists():
            return entries
        with open(self.log_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    entries.append(FeedbackEntry(**d))
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning("Skipping malformed feedback entry at line %d: %s", line_num, exc)
        return entries

    def calibration_report(
        self,
        confidence_band_width: float = 0.1,
        min_samples: int = 3,
    ) -> Dict[str, Any]:
        """
        Produce a calibration report bucketed by (violation, confidence band).

        Only includes buckets with >= min_samples entries — smaller buckets
        are too noisy to draw conclusions from.

        Returns a dict suitable for JSON serialisation.
        """
        entries = self.load_entries()
        if not entries:
            return {"status": "no_data", "buckets": [], "total_entries": 0}

        # Group by (violation, confidence band)
        bucket_data: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"agree": 0, "disagree": 0, "uncertain": 0}
        )

        for e in entries:
            # Quantise confidence to band
            band_low  = int(e.ai_confidence / confidence_band_width) * confidence_band_width
            band_high = band_low + confidence_band_width
            band_key  = f"{e.violation}|{band_low:.1f}-{band_high:.1f}"
            bucket_data[band_key][e.human_judgment] += 1

        buckets: List[Dict[str, Any]] = []
        for key, counts in sorted(bucket_data.items()):
            violation, band = key.split("|", 1)
            total = sum(counts.values())
            if total < min_samples:
                continue
            disagree_rate = counts["disagree"] / total
            note = ""
            if disagree_rate > self.HIGH_DISAGREEMENT_THRESHOLD:
                note = (
                    f"⚠️  High disagreement ({disagree_rate:.0%}). "
                    f"Consider reviewing the {violation} detection threshold "
                    f"for confidence band {band}."
                )
            buckets.append({
                "violation":         violation,
                "confidence_band":   band,
                "total":             total,
                "agree":             counts["agree"],
                "disagree":          counts["disagree"],
                "uncertain":         counts["uncertain"],
                "disagreement_rate": round(disagree_rate, 4),
                "note":              note,
            })

        # Sort by disagreement_rate descending (worst-calibrated first)
        buckets.sort(key=lambda b: b["disagreement_rate"], reverse=True)

        return {
            "status":           "ok",
            "total_entries":    len(entries),
            "buckets":          buckets,
            "high_disagreement_threshold": self.HIGH_DISAGREEMENT_THRESHOLD,
        }

    def summary_stats(self) -> Dict[str, Any]:
        """Quick overall stats: total entries, counts by judgment type."""
        entries = self.load_entries()
        counts: Dict[str, int] = {"agree": 0, "disagree": 0, "uncertain": 0}
        for e in entries:
            counts[e.human_judgment] = counts.get(e.human_judgment, 0) + 1
        total = len(entries)
        return {
            "total": total,
            "agree": counts["agree"],
            "disagree": counts["disagree"],
            "uncertain": counts["uncertain"],
            "overall_disagreement_rate": (
                round(counts["disagree"] / total, 4) if total > 0 else 0.0
            ),
        }
