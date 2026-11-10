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
from app.workers.safety_monitor import SafetyMonitor, SNAPSHOT_RECHECK_INTERVAL


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

    stills: list = []
    still_calls = 0

    async def get_camera_stills(self, vehicle_id, start_time, end_time):
        self.still_calls += 1
        return list(self.stills)


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
        initial.payload_json = {**initial.payload_json, "sent_chat_id": -100, "sent_message_id": 5}

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


def _still(camera: str, captured: datetime) -> dict:
    return {
        "input": camera,
        "url": f"https://s3.samsara.com/{camera}-{captured.timestamp()}.jpeg",
        "mediaType": "image",
        "capturedAt": captured.isoformat().replace("+00:00", "Z"),
    }


def test_pick_still_prefers_first_capture_after_event() -> None:
    event = datetime(2026, 11, 8, 12, 9, 7, tzinfo=UTC)
    before = event - timedelta(minutes=3)
    after = event + timedelta(minutes=2)
    later = event + timedelta(minutes=8)
    stills = [
        _still("dashcamDriverFacing", after), _still("dashcamRoadFacing", after),
        _still("dashcamRoadFacing", before), _still("dashcamRoadFacing", later),
    ]
    media, is_after = SafetyMonitor._pick_still(stills, event)
    assert is_after
    assert [m["input"] for m in media] == ["dashcamRoadFacing", "dashcamDriverFacing"]
    assert all(m["capturedAt"].startswith("2026-11-08T12:11:07") for m in media)

    media, is_after = SafetyMonitor._pick_still([_still("dashcamRoadFacing", before)], event)
    assert not is_after and media


async def test_obstructed_camera_alert_waits_for_still_then_sends_with_it(tmp_path) -> None:
    raw = _raw_event("obs", label="ObstructedCamera", minutes_ago=1)
    raw["media"] = [{"input": "MEDIA_INPUT_UNKNOWN", "url": ""}]
    client = FakeClient([[], [raw], [raw]])
    monitor, sessions = await _monitor(tmp_path, client)
    await monitor.poll_once()  # baseline

    event_time = datetime.fromisoformat(raw["startMs"].replace("Z", "+00:00"))
    client.stills = [_still("dashcamRoadFacing", event_time - timedelta(minutes=4))]
    await monitor.poll_once()  # only an older still so far: alert is held
    async with sessions() as session:
        held = await session.scalar(select(NotificationOutbox))
        row = await session.scalar(select(SafetyEvent))
    assert held.next_attempt_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(minutes=5)
    assert row.raw_event_json["snapshotInfo"]["afterEvent"] is False

    # The next periodic still (after the event) is uploaded.
    client.stills.append(_still("dashcamRoadFacing", event_time + timedelta(minutes=2)))
    client.stills.append(_still("dashcamDriverFacing", event_time + timedelta(minutes=2)))
    from app.faults.service import state_get, state_set
    import json
    async with sessions.begin() as session:  # skip the 30 s recheck wait
        key = "safety_snapshot:obs"
        stored = json.loads(await state_get(session, key))
        stored["checkedAt"] = (datetime.now(UTC) - SNAPSHOT_RECHECK_INTERVAL).isoformat()
        await state_set(session, key, json.dumps(stored))
    await monitor.poll_once()

    async with sessions() as session:
        released = await session.scalar(select(NotificationOutbox))
        row = await session.scalar(select(SafetyEvent))
    assert released.next_attempt_at.replace(tzinfo=UTC) <= datetime.now(UTC)
    assert [m["input"] for m in row.media_json] == ["dashcamRoadFacing", "dashcamDriverFacing"]
    assert row.raw_event_json["snapshotInfo"]["afterEvent"] is True
    text = safety_message(
        SimpleNamespace(**{c: getattr(row, c) for c in (
            "severity", "primary_behavior", "behavior_labels_json", "alert_id",
            "event_timestamp", "location_name", "latitude", "longitude",
            "raw_event_json", "review_status", "coaching_status")}, driver=None, vehicle=None),
        "America/New_York",
    )
    assert "📸 <b>Camera snapshot:</b>" in text and "2 min after the event" in text


def test_pick_still_uses_exact_safety_event_still_first() -> None:
    event = datetime(2026, 11, 8, 16, 40, 33, 53000, tzinfo=UTC)
    exact = _still("dashcamDriverFacing", event) | {"triggerReason": "safetyEvent"}
    periodic = [
        _still("dashcamRoadFacing", event + timedelta(minutes=2)) | {"triggerReason": "periodicStill"},
        _still("dashcamDriverFacing", event + timedelta(minutes=2)) | {"triggerReason": "periodicStill"},
    ]
    media, is_after = SafetyMonitor._pick_still(periodic + [exact], event)
    assert is_after and media == [exact]


class FakeBot:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def _message(self, chat_id):
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=len(self.calls) + 100)

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return self._message(kwargs["chat_id"])

    async def edit_message_text(self, **kwargs):
        self.calls.append(("edit_message_text", kwargs))

    async def edit_message_caption(self, **kwargs):
        self.calls.append(("edit_message_caption", kwargs))


async def test_speed_update_edits_the_original_alert(tmp_path) -> None:
    from cryptography.fernet import Fernet
    from app.database.models import SamsaraAccount, TelegramRoute
    from app.notifications.worker import OutboxWorker
    from app.safety.service import process_safety_event, queue_speed_update

    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'edit.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="acb", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        session.add(TelegramRoute(samsara_account_id=account.id, chat_id=-100, thread_id=None,
                                  purpose="all", active=True, created_at=now, updated_at=now))
        await session.flush()
        parsed = parse_safety_event(_raw_event("e1"))
        row, _ = await process_safety_event(session, parsed, initialization=False, send_initial=False,
                                            timezone="America/New_York", integration_id=account.id,
                                            notification_delay_seconds=0)
        event_id = row.id

    settings = Settings(token_encryption_key=Fernet.generate_key().decode())
    bot = FakeBot()
    worker = OutboxWorker(settings, sessions, bot)
    assert await worker.send_pending_once() == 1
    assert [c[0] for c in bot.calls] == ["send_message"]
    original_id = 101  # FakeBot numbers messages from 101

    async with sessions.begin() as session:
        row = await session.get(SafetyEvent, event_id)
        details = {"maxSpeedMph": 80.0, "postedSpeedLimitMph": 65.0, "overLimitMph": 15.0, "durationSeconds": 104}
        row.raw_event_json = {**row.raw_event_json, "speedingDetails": details}
        assert await queue_speed_update(session, row, details, 1)

    await worker.send_pending_once()
    kinds = [c[0] for c in bot.calls]
    assert kinds == ["send_message", "edit_message_text", "send_message"]
    edit = bot.calls[1][1]
    assert edit["message_id"] == original_id and edit["chat_id"] == -100
    assert "Posted limit:</b> 65 mph" in edit["text"]
    reply = bot.calls[2][1]
    assert reply["reply_to_message_id"] == original_id
    assert reply["text"] == (
        "🔄 <b>Speeding details update</b>\n"
        "<b>⚠️ LOW ALERT: LIGHT SPEEDING</b>\n"
        f"<b>Alert ID:</b> {row.alert_id}\n"
        "🚛 <b>Maximum speed:</b> 80 mph\n"
        "🚦 <b>Posted limit:</b> 65 mph\n"
        "⚡ <b>Over limit:</b> 15 mph\n"
        "⏱ <b>Duration:</b> 104 seconds"
    )
    async with sessions() as session:
        statuses = {r.notification_type: r.status for r in (await session.scalars(select(NotificationOutbox))).all()}
    assert statuses == {"new": "sent", "speed_update": "sent"}


async def test_speed_update_for_older_alert_replies_without_edit(tmp_path) -> None:
    from cryptography.fernet import Fernet
    from app.database.models import SamsaraAccount, TelegramRoute
    from app.notifications.worker import OutboxWorker
    from app.safety.service import process_safety_event, queue_speed_update

    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'edit.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="acb", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        session.add(TelegramRoute(samsara_account_id=account.id, chat_id=-100, thread_id=None,
                                  purpose="all", active=True, created_at=now, updated_at=now))
        await session.flush()
        parsed = parse_safety_event(_raw_event("e1"))
        row, _ = await process_safety_event(session, parsed, initialization=False, send_initial=False,
                                            timezone="America/New_York", integration_id=account.id,
                                            notification_delay_seconds=0)
        event_id = row.id

    settings = Settings(token_encryption_key=Fernet.generate_key().decode())
    bot = FakeBot()
    worker = OutboxWorker(settings, sessions, bot)
    assert await worker.send_pending_once() == 1
    assert [c[0] for c in bot.calls] == ["send_message"]
    original_id = 101  # FakeBot numbers messages from 101

    async with sessions.begin() as session:
        row = await session.get(SafetyEvent, event_id)
        details = {"maxSpeedMph": 80.0, "postedSpeedLimitMph": 65.0, "overLimitMph": 15.0, "durationSeconds": 104}
        row.raw_event_json = {**row.raw_event_json, "speedingDetails": details}
        assert await queue_speed_update(session, row, details, 1)

    async with sessions.begin() as session:  # simulate an alert sent before refs were stored
        first = await session.scalar(select(NotificationOutbox).where(NotificationOutbox.notification_type == "new"))
        first.payload_json = {k: v for k, v in first.payload_json.items() if not k.startswith("sent_")}

    await worker.send_pending_once()
    assert [c[0] for c in bot.calls] == ["send_message", "send_message"]
    update = bot.calls[1][1]
    assert update["reply_to_message_id"] == original_id
    assert update["text"].startswith("🔄 <b>Speeding details update</b>")
    assert "Review status" not in update["text"]


def test_each_alert_type_has_its_topic() -> None:
    from app.safety.topics import topic_for
    assert topic_for("LightSpeeding").key == "speeding"
    assert topic_for("SevereSpeeding").key == "speeding"
    assert topic_for("MobileUsage").key == "distraction"
    assert topic_for("NoSeatbelt").key == "seatbelt"
    assert topic_for("ObstructedCamera").key == "camera"
    assert topic_for("Drowsy").key == "distraction"
    assert topic_for("Smoking").key == "distraction"
    assert topic_for("SomethingNew").key == "other"


async def test_safety_alert_goes_only_to_its_type_topic(tmp_path) -> None:
    from cryptography.fernet import Fernet
    from app.database.models import SamsaraAccount, TelegramRoute
    from app.notifications.worker import OutboxWorker
    from app.safety.service import process_safety_event

    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'route.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="acb", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        for purpose, thread in (("fault", 1), ("s:speeding", 2), ("s:distraction", 3), ("s:other", 4)):
            session.add(TelegramRoute(samsara_account_id=account.id, chat_id=-100, thread_id=thread,
                                      purpose=purpose, active=True, created_at=now, updated_at=now))
        await session.flush()
        for event_id, label in (("a", "HeavySpeeding"), ("b", "MobileUsage"), ("c", "UnsafeParking")):
            await process_safety_event(session, parse_safety_event(_raw_event(event_id, label=label)),
                                       initialization=False, send_initial=False, timezone="America/New_York",
                                       integration_id=account.id, notification_delay_seconds=0)

    bot = FakeBot()
    await OutboxWorker(Settings(token_encryption_key=Fernet.generate_key().decode()), sessions, bot).send_pending_once()
    sent = sorted((c[1]["message_thread_id"], c[1]["text"].split("\n")[0]) for c in bot.calls)
    assert sent == [
        (2, "<b>⚠️ HIGH ALERT: HEAVY SPEEDING</b>"),
        (3, "<b>⚠️ MODERATE ALERT: MOBILE USAGE</b>"),
        (4, "<b>⚠️ LOW ALERT: UNSAFE PARKING</b>"),
    ]


async def test_migrated_group_stops_failing_and_uses_topics(tmp_path) -> None:
    """Old basic group (-100) became a supergroup (-1009) that already has topics."""
    from aiogram.exceptions import TelegramMigrateToChat
    from aiogram.methods import SendMessage
    from cryptography.fernet import Fernet
    from app.database.models import SamsaraAccount, TelegramRoute
    from app.notifications.worker import OutboxWorker
    from app.safety.service import process_safety_event

    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="acb", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        session.add(TelegramRoute(samsara_account_id=account.id, chat_id=-100, thread_id=None,
                                  purpose="all", active=True, created_at=now, updated_at=now))
        session.add(TelegramRoute(samsara_account_id=account.id, chat_id=-1009, thread_id=7,
                                  purpose="s:speeding", active=True, created_at=now, updated_at=now))
        await session.flush()
        await process_safety_event(session, parse_safety_event(_raw_event("m1")), initialization=False,
                                   send_initial=False, timezone="America/New_York",
                                   integration_id=account.id, notification_delay_seconds=0)

    class MigratingBot(FakeBot):
        async def send_message(self, **kwargs):
            if kwargs["chat_id"] == -100:
                raise TelegramMigrateToChat(method=SendMessage(chat_id=-100, text="x"),
                                            message="group upgraded", migrate_to_chat_id=-1009)
            return await super().send_message(**kwargs)

    bot = MigratingBot()
    worker = OutboxWorker(Settings(token_encryption_key=Fernet.generate_key().decode()), sessions, bot)
    for _ in range(3):
        await worker.send_pending_once()

    assert [(c[1]["chat_id"], c[1]["message_thread_id"]) for c in bot.calls] == [(-1009, 7)]
    async with sessions() as session:
        routes = {(r.chat_id, r.thread_id): r.active for r in (await session.scalars(select(TelegramRoute))).all()}
        statuses = sorted(r.status for r in (await session.scalars(select(NotificationOutbox))).all())
    assert routes == {(-100, None): False, (-1009, 7): True}
    assert statuses == ["dead", "sent"]  # the old-group copy is dropped, not retried forever


async def test_migration_moves_routes_when_new_group_has_none(tmp_path) -> None:
    from app.bot.routing import migrate_chat_routes
    from app.database.models import SamsaraAccount, TelegramRoute

    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'mig2.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="acb", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        session.add(TelegramRoute(samsara_account_id=account.id, chat_id=-100, thread_id=None,
                                  purpose="all", active=True, created_at=now, updated_at=now))
    async with sessions.begin() as session:
        assert await migrate_chat_routes(session, -100, -1009) == (1, 0)
    async with sessions() as session:
        route = await session.scalar(select(TelegramRoute))
    assert (route.chat_id, route.active) == (-1009, True)


async def test_upgrade_does_not_backfill_alerts_sent_by_old_version(tmp_path) -> None:
    client = FakeClient([[], [_raw_event("old")]])
    client.speeding = {"maxSpeedMph": 76.0}
    monitor, sessions = await _monitor(tmp_path, client)
    await monitor.poll_once()
    await monitor.poll_once()
    async with sessions.begin() as session:
        initial = await session.scalar(select(NotificationOutbox))
        initial.status = "sent"  # sent by the old version: no stored message reference

    client.speeding = {"maxSpeedMph": 76.0, "postedSpeedLimitMph": 65.0}
    monitor._last_speed_recheck = None
    from app.faults.service import state_set
    async with sessions.begin() as session:
        await state_set(session, "safety_speed_details:old", "")
    await monitor.recheck_speed_limits()

    async with sessions() as session:
        types = [r.notification_type for r in (await session.scalars(select(NotificationOutbox))).all()]
    assert types == ["new"]


async def test_obstructed_alert_not_held_without_camera_read_access(tmp_path, caplog) -> None:
    from app.samsara.exceptions import SamsaraPermissionError

    class NoCameraClient(FakeClient):
        async def get_camera_stills(self, vehicle_id, start_time, end_time):
            self.still_calls += 1
            raise SamsaraPermissionError("no camera scope")

    client = NoCameraClient([[], [_raw_event("o1", label="ObstructedCamera"), _raw_event("o2", label="ObstructedCamera")]])
    monitor, sessions = await _monitor(tmp_path, client)
    await monitor.poll_once()
    await monitor.poll_once()

    async with sessions() as session:
        due = [r.next_attempt_at.replace(tzinfo=UTC) for r in (await session.scalars(select(NotificationOutbox))).all()]
    assert len(due) == 2 and all(d <= datetime.now(UTC) for d in due)  # sent now, not held 8 min
    assert client.still_calls == 1  # paused after the first permission error
    assert "camera_stills_unauthorized" in caplog.text
