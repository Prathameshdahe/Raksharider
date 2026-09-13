import logging
from datetime import datetime, timezone
from typing import Any, Dict

from app.database.supabase import supabase
from app.schemas.violation import ViolationRecord

logger = logging.getLogger(__name__)


class ViolationService:

    @staticmethod
    def save_violation(
        violation: ViolationRecord
    ) -> Dict[str, Any]:
        """
        Save one finalized violation to PostgreSQL.
        """

        try:
            violation_data = {
                "report_id": violation.report_id,
                "violation_type": violation.violation_type,
                "confidence": violation.confidence,
                "created_at": (
                    violation.created_at.isoformat()
                    if violation.created_at
                    else datetime.now(timezone.utc).isoformat()
                )
            }

            response = (
                supabase
                .table("violations")
                .insert(violation_data)
                .execute()
            )

            if not response.data:
                raise RuntimeError(
                    "Violation insert succeeded but returned no data"
                )

            saved_violation = response.data[0]

            logger.info(
                "Violation saved successfully: %s",
                saved_violation.get("id")
            )

            return {
                "violation_id": saved_violation["id"],
                "report_id": saved_violation["report_id"],
                "violation_type": saved_violation["violation_type"],
                "confidence": saved_violation["confidence"]
            }

        except Exception as e:
            logger.exception(
                "Failed to save violation: %s",
                str(e)
            )
            raise RuntimeError(
                f"Failed to save violation: {str(e)}"
            )