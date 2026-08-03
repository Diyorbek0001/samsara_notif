from app.safety.service import media_signature


def test_media_signature_ignores_expiring_url_values() -> None:
    first = [{"input": "road", "url": "https://example.invalid/one"}]
    refreshed = [{"input": "road", "url": "https://example.invalid/two"}]

    assert media_signature(first) == media_signature(refreshed)


def test_media_signature_changes_when_new_camera_video_appears() -> None:
    road_only = [{"input": "road", "url": "https://example.invalid/road"}]
    road_and_driver = road_only + [
        {"input": "driver", "url": "https://example.invalid/driver"}
    ]

    assert media_signature(road_only) != media_signature(road_and_driver)


def test_media_signature_requires_a_downloadable_url() -> None:
    assert media_signature([]) is None
    assert media_signature([{"input": "road", "url": None}]) is None
