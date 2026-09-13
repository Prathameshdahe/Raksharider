from typing import Dict, List, Tuple

from app.schemas.alert import Alert
from app.schemas.temporary_event import TemporaryEvent
from app.schemas.finalized_event import FinalizedEvent


class EventBuffer:
    def __init__(self, event_window: float = 5.0):
        self.event_window = event_window
        self.events: Dict[Tuple[str, str], TemporaryEvent] = {}

    def add_alert(self, alert: Alert) -> None:
        """Add an alert to the RAM event buffer."""

        key = (alert.tracking_id, alert.alert_type)
        existing_event = self.events.get(key)

        # No existing event -> create one.
        if existing_event is None:
            self.events[key] = TemporaryEvent(
                tracking_id=alert.tracking_id,
                alert_type=alert.alert_type,
                first_seen=alert.timestamp,
                last_seen=alert.timestamp,
                observation_count=1,
                best_confidence=alert.confidence,
                evidence=[alert.evidence] if alert.evidence else []
            )
            return

        # Same active event -> update it.
        if alert.timestamp - existing_event.last_seen <= self.event_window:
            existing_event.last_seen = alert.timestamp
            existing_event.observation_count += 1

            existing_event.best_confidence = max(
                existing_event.best_confidence,
                alert.confidence
            )

            if (
                alert.evidence
                and alert.evidence not in existing_event.evidence
            ):
                existing_event.evidence.append(alert.evidence)

        # Previous event expired -> start a new event.
        else:
            self.events[key] = TemporaryEvent(
                tracking_id=alert.tracking_id,
                alert_type=alert.alert_type,
                first_seen=alert.timestamp,
                last_seen=alert.timestamp,
                observation_count=1,
                best_confidence=alert.confidence,
                evidence=[alert.evidence] if alert.evidence else []
            )

    def get_active_events(self) -> List[TemporaryEvent]:
        """Return events currently stored in RAM."""
        return list(self.events.values())

    def finalize_expired_events(
        self,
        current_timestamp: float
    ) -> List[FinalizedEvent]:
        """Finalize events whose inactivity exceeds the event window."""

        finalized: List[FinalizedEvent] = []

        for key, event in list(self.events.items()):
            if current_timestamp - event.last_seen > self.event_window:

                finalized_event = FinalizedEvent(
                    tracking_id=event.tracking_id,
                    alert_type=event.alert_type,
                    first_seen=event.first_seen,
                    last_seen=event.last_seen,
                    observation_count=event.observation_count,
                    best_confidence=event.best_confidence,
                    evidence=event.evidence
                )

                finalized.append(finalized_event)

                # Remove finalized event from RAM.
                del self.events[key]

        return finalized

    def finalize_all(self) -> List[FinalizedEvent]:
        """Finalize all active events and clear the RAM buffer."""

        finalized: List[FinalizedEvent] = []

        for event in self.events.values():
            finalized.append(
                FinalizedEvent(
                    tracking_id=event.tracking_id,
                    alert_type=event.alert_type,
                    first_seen=event.first_seen,
                    last_seen=event.last_seen,
                    observation_count=event.observation_count,
                    best_confidence=event.best_confidence,
                    evidence=event.evidence
                )
            )

        self.events.clear()

        return finalized