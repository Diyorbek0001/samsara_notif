from app.samsara.safety_parser import parse_safety_event, severity_for


def test_observed_safety_stream_shape():
    event = parse_safety_event(
        {
            "id": "event-1",
            "startMs": 1784904060000,
            "eventState": "needsReview",
            "behaviorLabels": [{"label": "RollingStop", "source": "dashcam"}],
            "contextLabels": [],
            "asset": {"id": "vehicle-1", "name": "Unit 1", "vin": "TESTVIN"},
            "driver": {"id": "driver-1"},
            "location": {"latitude": 32.1, "longitude": -96.8, "address": "Test City"},
            "media": [
                {"input": "MEDIA_INPUT_PRIMARY", "url": "https://example.invalid/video.mp4"},
                {"input": "MEDIA_INPUT_UNKNOWN", "url": ""},
            ],
        }
    )
    assert event is not None
    assert event.samsara_event_id == "event-1"
    assert event.primary_behavior == "RollingStop"
    assert event.severity == "moderate"
    assert event.vehicle_name == "Unit 1"
    assert len(event.media) == 2
    assert event.media[1]["url"] is None


def test_safety_severity_mapping():
    assert severity_for(["Crash"]) == "critical"
    assert severity_for(["SevereSpeeding"]) == "high"
    assert severity_for(["Braking"]) == "moderate"
    assert severity_for(["ObstructedCamera"]) == "low"
    assert severity_for(["NewUnknownBehavior"]) == "unknown"


def test_invalid_safety_event_is_tolerated():
    assert parse_safety_event({"id": "missing-time"}) is None


def test_rfc3339_start_ms_is_supported():
    event = parse_safety_event(
        {
            "id": "event-rfc3339",
            "startMs": "2026-07-30T12:54:45.720Z",
            "behaviorLabels": [{"label": "Braking"}],
        }
    )
    assert event is not None
    assert event.event_timestamp.isoformat() == "2026-07-30T12:54:45.720000+00:00"


def test_structured_address_is_formatted_for_notification():
    event = parse_safety_event(
        {
            "id": "event-address",
            "startMs": "2026-07-30T12:54:45.720Z",
            "location": {
                "address": {
                    "streetNumber": "2123",
                    "street": "Harrison Drive",
                    "city": "Clinton",
                    "state": "IA",
                    "postalCode": "52730",
                    "country": "US",
                }
            },
        }
    )
    assert event is not None
    assert event.location_name == "2123 Harrison Drive, Clinton, IA 52730, US"


def test_snapshot_media_type_is_preserved():
    event = parse_safety_event(
        {
            "id": "event-snapshot",
            "startMs": "2026-08-03T18:30:00Z",
            "behaviorLabels": [{"label": "ObstructedCamera"}],
            "media": [
                {
                    "input": "dashcamDriverFacing",
                    "url": "https://example.invalid/snapshot.jpg",
                    "mediaType": "image",
                }
            ],
        }
    )
    assert event is not None
    assert event.media[0]["mediaType"] == "image"
