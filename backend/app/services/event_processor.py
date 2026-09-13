from typing import Any, Dict

from app.schemas.finalized_event import FinalizedEvent
from app.schemas.violation import ViolationRecord
from app.schemas.evidence_frame import EvidenceFrameRecord

from app.services.violation_service import ViolationService
from app.services.evidence_service import EvidenceService


class EventProcessor:

    @staticmethod
    def process_event(
        report_id: int,
        event: FinalizedEvent
    ) -> Dict[str, Any]:
        """
        Convert one finalized event into persistent violation
        and evidence records.
        """

        violation = ViolationRecord(
            report_id=report_id,
            violation_type=event.alert_type,
            confidence=event.best_confidence
        )

        saved_violation = ViolationService.save_violation(
            violation
        )

        saved_evidence = []

        for evidence in event.evidence:
            evidence_record = EvidenceFrameRecord(
                report_id=report_id,
                storage_url=evidence,
                frame_timestamp=event.last_seen
            )

            result = EvidenceService.save_evidence(
                evidence_record
            )

            saved_evidence.append(result)

        return {
            "violation": saved_violation,
            "evidence": saved_evidence
        }