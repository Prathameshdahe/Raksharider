from typing import List

from app.schemas.alert import Alert
from app.schemas.finalized_event import FinalizedEvent

from app.services.event_buffer import EventBuffer
from app.services.event_processor import EventProcessor


class AlertPipeline:
    def __init__(self, event_window: float = 5.0):
        self.buffer = EventBuffer(event_window=event_window)

    def process_alert(
        self,
        report_id: int,
        alert: Alert
    ) -> List[FinalizedEvent]:
        """
        Add one alert to the RAM buffer and finalize events
        that have expired.
        """

        self.buffer.add_alert(alert)

        finalized_events = self.buffer.finalize_expired_events(
            current_timestamp=alert.timestamp
        )

        for event in finalized_events:
            EventProcessor.process_event(
                report_id=report_id,
                event=event
            )

        return finalized_events

    def finish(
        self,
        report_id: int
    ) -> List[FinalizedEvent]:
        """
        Finalize all remaining events when processing ends.
        """

        finalized_events = self.buffer.finalize_all()

        for event in finalized_events:
            EventProcessor.process_event(
                report_id=report_id,
                event=event
            )

        return finalized_events