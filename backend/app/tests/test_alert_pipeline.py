from unittest.mock import patch

from app.schemas.alert import Alert
from app.services.alert_pipeline import AlertPipeline


def test_process_alert_buffers_alert():
    pipeline = AlertPipeline(event_window=5.0)

    alert = Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.94,
        evidence="frame_100.jpg"
    )

    with patch(
        "app.services.alert_pipeline.EventProcessor.process_event"
    ) as mock_process:

        finalized = pipeline.process_alert(
            report_id=42,
            alert=alert
        )

    assert finalized == []
    assert len(pipeline.buffer.get_active_events()) == 1

    mock_process.assert_not_called()


def test_process_alert_finalizes_expired_event():
    pipeline = AlertPipeline(event_window=5.0)

    first_alert = Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.91,
        evidence="frame_100.jpg"
    )

    second_alert = Alert(
        tracking_id="202",
        alert_type="triple_riding",
        timestamp=16.0,
        confidence=0.88,
        evidence="frame_160.jpg"
    )

    with patch(
        "app.services.alert_pipeline.EventProcessor.process_event"
    ) as mock_process:

        pipeline.process_alert(
            report_id=42,
            alert=first_alert
        )

        finalized = pipeline.process_alert(
            report_id=42,
            alert=second_alert
        )

    assert len(finalized) == 1
    assert finalized[0].tracking_id == "101"
    assert finalized[0].alert_type == "no_helmet"

    mock_process.assert_called_once()

    processed_event = mock_process.call_args.kwargs["event"]

    assert processed_event.tracking_id == "101"
    assert processed_event.alert_type == "no_helmet"


def test_finish_finalizes_remaining_events():
    pipeline = AlertPipeline(event_window=5.0)

    alert = Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.94,
        evidence="frame_100.jpg"
    )

    with patch(
        "app.services.alert_pipeline.EventProcessor.process_event"
    ) as mock_process:

        pipeline.process_alert(
            report_id=42,
            alert=alert
        )

        finalized = pipeline.finish(report_id=42)

    assert len(finalized) == 1
    assert finalized[0].tracking_id == "101"

    assert len(pipeline.buffer.get_active_events()) == 0

    mock_process.assert_called_once()