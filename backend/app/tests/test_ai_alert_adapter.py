from app.services.ai_alert_adapter import AIAlertAdapter


def test_ai_output_converts_to_alerts():
    ai_output = {
        "alerts": [
            {
                "tracking_id": "101",
                "alert_type": "no_helmet",
                "timestamp": 10.5,
                "confidence": 0.94,
                "evidence": "frame_100.jpg"
            },
            {
                "tracking_id": "101",
                "alert_type": "triple_riding",
                "timestamp": 10.5,
                "confidence": 0.88,
                "evidence": "frame_100.jpg"
            }
        ]
    }

    alerts = AIAlertAdapter.to_alerts(ai_output)

    assert len(alerts) == 2

    assert alerts[0].tracking_id == "101"
    assert alerts[0].alert_type == "no_helmet"
    assert alerts[0].timestamp == 10.5
    assert alerts[0].confidence == 0.94
    assert alerts[0].evidence == "frame_100.jpg"

    assert alerts[1].tracking_id == "101"
    assert alerts[1].alert_type == "triple_riding"
    assert alerts[1].timestamp == 10.5
    assert alerts[1].confidence == 0.88
    assert alerts[1].evidence == "frame_100.jpg"