from unittest.mock import MagicMock, patch

from app.schemas.evidence_frame import EvidenceFrameRecord
from app.services.evidence_service import EvidenceService


def test_save_evidence():
    evidence = EvidenceFrameRecord(
        report_id=42,
        storage_url="evidence/frame_100.jpg",
        frame_timestamp=10.5
    )

    fake_response = MagicMock()
    fake_response.data = [{
        "id": 201,
        "report_id": 42,
        "storage_url": "evidence/frame_100.jpg",
        "frame_timestamp": 10.5
    }]

    with patch(
        "app.services.evidence_service.supabase"
    ) as mock_supabase:

        mock_supabase.table.return_value.insert.return_value.execute.return_value = (
            fake_response
        )

        result = EvidenceService.save_evidence(evidence)

        assert result["evidence_id"] == 201
        assert result["report_id"] == 42
        assert result["storage_url"] == "evidence/frame_100.jpg"
        assert result["frame_timestamp"] == 10.5

        mock_supabase.table.assert_called_once_with("evidence_frames")