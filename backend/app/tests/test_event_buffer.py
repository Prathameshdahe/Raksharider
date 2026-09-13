from app.schemas.alert import Alert
from app.schemas.finalized_event import FinalizedEvent
from app.services.event_buffer import EventBuffer


def test_new_alert_creates_event():
    buffer = EventBuffer(event_window=5.0)

    alert = Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.91,
        evidence="frame_100.jpg"
    )

    buffer.add_alert(alert)

    events = buffer.get_active_events()

    assert len(events) == 1
    assert events[0].tracking_id == "101"
    assert events[0].alert_type == "no_helmet"
    assert events[0].observation_count == 1


def test_same_alert_is_aggregated():
    buffer = EventBuffer(event_window=5.0)

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.91,
        evidence="frame_100.jpg"
    ))

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=11.0,
        confidence=0.94,
        evidence="frame_110.jpg"
    ))

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=12.0,
        confidence=0.89,
        evidence="frame_120.jpg"
    ))

    events = buffer.get_active_events()

    assert len(events) == 1
    assert events[0].observation_count == 3
    assert events[0].best_confidence == 0.94
    assert len(events[0].evidence) == 3


def test_different_alert_types_create_separate_events():
    buffer = EventBuffer(event_window=5.0)

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.91
    ))

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="triple_riding",
        timestamp=10.5,
        confidence=0.88
    ))

    events = buffer.get_active_events()

    assert len(events) == 2


def test_expired_event_is_finalized():
    buffer = EventBuffer(event_window=5.0)

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.91
    ))

    finalized = buffer.finalize_expired_events(current_timestamp=16.0)

    assert len(finalized) == 1
    assert finalized[0].tracking_id == "101"
    assert len(buffer.get_active_events()) == 0


def test_expired_event_becomes_finalized_event():
    buffer = EventBuffer(event_window=5.0)

    buffer.add_alert(Alert(
        tracking_id="101",
        alert_type="no_helmet",
        timestamp=10.0,
        confidence=0.91,
        evidence="frame_100.jpg"
    ))

    finalized = buffer.finalize_expired_events(current_timestamp=16.0)

    assert len(finalized) == 1

    event = finalized[0]

    assert isinstance(event, FinalizedEvent)
    assert event.tracking_id == "101"
    assert event.alert_type == "no_helmet"
    assert event.first_seen == 10.0
    assert event.last_seen == 10.0
    assert event.observation_count == 1
    assert event.best_confidence == 0.91
    assert event.evidence == ["frame_100.jpg"]

    # The finalized event must be removed from RAM.
    assert len(buffer.get_active_events()) == 0