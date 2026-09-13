from typing import Any, Dict, List

from app.schemas.alert import Alert


class AIAlertAdapter:

    @staticmethod
    def to_alerts(ai_output: Dict[str, Any]) -> List[Alert]:
        """
        Convert normalized AI output into our stable Alert schema.

        The AI model can change internally; this adapter is the boundary
        between the AI output and the backend event-processing system.
        """

        alerts: List[Alert] = []

        detections = ai_output.get("alerts", [])

        for detection in detections:
            alert = Alert(
                tracking_id=str(detection["tracking_id"]),
                alert_type=str(detection["alert_type"]),
                timestamp=float(detection["timestamp"]),
                confidence=float(detection["confidence"]),
                evidence=detection.get("evidence")
            )

            alerts.append(alert)

        return alerts