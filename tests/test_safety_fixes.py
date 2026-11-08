from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.bot.safety_formatters import behavior_label, safety_message
from app.config import Settings
from app.database.models import NotificationOutbox, SafetyEvent
from app.database.session import create_database, create_tables
from app.samsara.exceptions import SamsaraAuthenticationError
from app.samsara.safety_models import SafetyStreamPage
from app.samsara.safety_parser import parse_safety_event, severity_for
from app.workers.safety_monitor import SafetyMonitor


def _raw_event(event_id: str, label: str = "LightSpeeding", minutes_ago: int = 1) -> dict:
    start = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "id": event_id,
        "startMs": start.isoformat().replace("+00:00", "Z"),
        "tripStartTime": (start - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
        "asset": {"id": "281475000969003", "name": "Unit 4495"},
        "driver": {"id": "0"},
        "behaviorLabels": [{"label": label, "source": "SYSTEM"}],
        "eventState": "needsReview",
    }


def test_samsara_speeding_tiers_have_a_severity() -> None:
    assert severity_for(["LightSpeeding"]) == "low"
    assert severity_for(["ModerateSpeeding"]) == "moderate"
    assert severity_for(["HeavySpeeding"]) == "high"
    assert severity_for(["SevereSpeeding"]) == "high"
    assert severity_for(["Drinking"]) == "moderate"
    assert severity_for(["UnsafeParking"]) == "low"


def test_driver_id_zero_means_unassigned() -> None:
    parsed = parse_safety_event(_raw_event("e1"))
    assert parsed.driver_id is None


def test_alert_title_uses_readable_behavior_and_unassigned_driver() -> None:
    assert behavior_label("LightSpeeding") == "Light Speeding"
    event = SimpleNamespace(
        severity="low",
        primary_behavior="LightSpeeding",
        behavior_labels_json=["LightSpeeding"],
        alert_id="SAFE-1",
        driver=None,
        vehicle=None,
        event_timestamp=datetime.now(UTC),
        location_name=None,
        latitude=None,
        longitude=None,
        raw_event_json={},
        review_status="new",
        coaching_status="pending",
    )
    text = safety_message(event, "America/New_York")
    assert "LOW ALERT: LIGHT SPEEDING" in text
    assert "UNKNOWN" not in text
    assert "<b>Driver:</b> Unassigned" in text


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.starts: list[str] = []
        self.video_calls = 0
        self.speeding: dict | None = None

    async def get_safety_event_stream(self, *, start_time, end_time, after, query_by_time_field):
        self.starts.append(start_time)
        data = self.pages.pop(0) if self.pages else []
        return SafetyStreamPage(data=data, end_cursor="c", has_next_page=False)

    async def get_speeding_details(self, vehicle_id, event_time, trip_start=None):
        return self.speeding

    async def retrieve_camera_video(self, vehicle_id, event_time):
        self.video_calls += 1
        raise SamsaraAuthenticationError("Samsara API authentication failed")

    async def retrieve_camera_snapshots(self, vehicle_id, event_time):
        raise SamsaraAuthenticationError("Samsara API authentication failed")


async def _monitor(tmp_path, client):
    settings = Settings(safety_notification_delay_seconds=0)
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'safety.db'}")
    await create_tables(engine)
    return SafetyMonitor(settings, sessions, client, integration_id=1), sessions


async def test_second_poll_never_reaches_before_the_baseline_window(tmp_path) -> None:
    client = FakeClient([[], [_raw_event("old", minutes_ago=20)]])
    monitor, sessions = await _monitor(tmp_path, client)

    await monitor.poll_once()  # baseline: 10 minute lookback
    await monitor.poll_once()

    # Without the clamp the second start would be ~30 minutes back.
    assert client.starts[1] == client.starts[0]


async def test_media_permission_error_pauses_retrieval(tmp_path, caplog) -> None:
    client = FakeClient([[_raw_event("a"), _raw_event("b")]])
    monitor, _ = await _monitor(tmp_path, client)

    await monitor.poll_once()

    assert client.video_calls == 1  # second event skipped while paused
    assert "camera_media_retrieval_unauthorized" in caplog.text
    assert "Traceback" not in caplog.text


async def test_recheck_adds_posted_limit_after_trip_ends(tmp_path) -> None:
    client = FakeClient([[], [_raw_event("live")]])
    client.speeding = {"maxSpeedMph": 76.0, "source": "vehicleSpeedHistory"}
    monitor, sessions = await _monitor(tmp_path, client)
    await monitor.poll_once()  # baseline
    await monitor.poll_once()  # live event, only max speed known

    async with sessions.begin() as session:
        row = await session.scalar(select(SafetyEvent))
        assert row.raw_event_json["speedingDetails"].get("postedSpeedLimitMph") is None
        initial = await session.scalar(select(NotificationOutbox))
        initial.status = "sent"  # pretend the first alert went out

    # Trip ended: Samsara now has the speeding interval with the limit.
    client.speeding = {"maxSpeedMph": 76.0, "postedSpeedLimitMph": 65.0, "overLimitMph": 11.0}
    monitor._last_speed_recheck = None
    async with sessions.begin() as session:
        await session.execute(
            SafetyEvent.__table__.update().values(initialized_baseline=False)
        )
    from app.faults.service import state_set
    async with sessions.begin() as session:  # expire the 5 minute recheck cache
        await state_set(session, "safety_speed_details:live", "")

    assert await monitor.recheck_speed_limits() == 1

    async with sessions() as session:
        row = await session.scalar(select(SafetyEvent))
        assert row.raw_event_json["speedingDetails"]["postedSpeedLimitMph"] == 65.0
        types = [r.notification_type for r in (await session.scalars(select(NotificationOutbox))).all()]
    assert types == ["new", "speed_update"]


async def test_no_speed_update_for_alert_that_was_never_delivered(tmp_path) -> None:
    client = FakeClient([[], [_raw_event("dropped")]])
    client.speeding = {"maxSpeedMph": 76.0}
    monitor, sessions = await _monitor(tmp_path, client)
    await monitor.poll_once()
    await monitor.poll_once()
    async with sessions.begin() as session:
        initial = await session.scalar(select(NotificationOutbox))
        initial.status = "dead"  # no activated group when it was due

    client.speeding = {"maxSpeedMph": 76.0, "postedSpeedLimitMph": 65.0}
    monitor._last_speed_recheck = None
    from app.faults.service import state_set
    async with sessions.begin() as session:
        await state_set(session, "safety_speed_details:dropped", "")
    await monitor.recheck_speed_limits()

    async with sessions() as session:
        types = [r.notification_type for r in (await session.scalars(select(NotificationOutbox))).all()]
    assert types == ["new"]


def test_alert_links_to_samsara_safety_inbox() -> None:
    url = (
        "https://cloud.samsara.com/o/7005121/fleet/safety/event_review"
        "?aggregationUuid=abc&selectedAggregationKey=event"
    )
    event = SimpleNamespace(
        severity="moderate", primary_behavior="ModerateSpeeding",
        behavior_labels_json=["ModerateSpeeding"], alert_id="SAFE-1", driver=None,
        vehicle=None, event_timestamp=datetime.now(UTC), location_name=None,
        latitude=None, longitude=None, raw_event_json={"inboxEventUrl": url},
        review_status="new", coaching_status="pending",
    )
    text = safety_message(event, "America/New_York")
    assert '<a href="https://cloud.samsara.com/o/7005121/fleet/safety/event_review?aggregationUuid=abc&amp;selectedAggregationKey=event">Samsara Safety Inbox</a>' in text

    event.raw_event_json = {"inboxEventUrl": "https://evil.example/phish"}
    assert "Samsara Safety Inbox" not in safety_message(event, "America/New_York")
