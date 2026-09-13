from unittest.mock import patch

from app.schemas.finalized_event import FinalizedEvent
from app.services.event_processor import EventProcessor


def test_process_event():
    event = FinalizedEvent(
        tracking_id="101",
        alert_type="no_helmet",
        first_seen=10.0,
        last_seen=12.0,
        observation_count=3,
        best_confidence=0.94,
        evidence=[
            "evidence/frame_100.jpg",
            "evidence/frame_120.jpg"
        ]
    )

    fake_violation = {
        "violation_id": 501,
        "report_id": 42,
        "violation_type": "no_helmet",
        "confidence": 0.94
    }

    fake_evidence_1 = {
        "evidence_id": 601,
        "report_id": 42,
        "storage_url": "evidence/frame_100.jpg",
        "frame_timestamp": 12.0
    }

    fake_evidence_2 = {
        "evidence_id": 602,
        "report_id": 42,
        "storage_url": "evidence/frame_120.jpg",
        "frame_timestamp": 12.0
    }

    with patch(
        "app.services.event_processor.ViolationService.save_violation",
        return_value=fake_violation
    ) as mock_violation, patch(
        "app.services.event_processor.EvidenceService.save_evidence",
        side_effect=[fake_evidence_1, fake_evidence_2]
    ) as mock_evidence:

        result = EventProcessor.process_event(
            report_id=42,
            event=event
        )

    assert result["violation"] == fake_violation
    assert len(result["evidence"]) == 2
    assert result["evidence"][0] == fake_evidence_1
    assert result["evidence"][1] == fake_evidence_2

    mock_violation.assert_called_once()
    assert mock_evidence.call_count == 2