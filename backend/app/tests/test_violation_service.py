from unittest.mock import MagicMock, patch

from app.schemas.violation import ViolationRecord
from app.services.violation_service import ViolationService


def test_save_violation():
    violation = ViolationRecord(
        report_id=42,
        violation_type="no_helmet",
        confidence=0.94
    )

    fake_response = MagicMock()
    fake_response.data = [{
        "id": 101,
        "report_id": 42,
        "violation_type": "no_helmet",
        "confidence": 0.94
    }]

    with patch(
        "app.services.violation_service.supabase"
    ) as mock_supabase:

        mock_supabase.table.return_value.insert.return_value.execute.return_value = (
            fake_response
        )

        result = ViolationService.save_violation(violation)

        assert result["violation_id"] == 101
        assert result["report_id"] == 42
        assert result["violation_type"] == "no_helmet"
        assert result["confidence"] == 0.94

        mock_supabase.table.assert_called_once_with("violations")