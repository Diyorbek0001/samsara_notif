"""Routes exactly as the production bot created them before topics existed.

The upgrade must keep delivering to these: plain groups (no thread) and a
single forum topic, with the legacy purposes fault / safety / both / all /
fuel. Fuel was removed and must simply receive nothing.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import Settings
from app.database.models import NotificationOutbox, SamsaraAccount, TelegramRoute
from app.database.session import create_database, create_tables
from app.faults.service import process_fault
from app.notifications.worker import OutboxWorker
from app.safety.service import process_safety_event
from app.samsara.parsers import parse_vehicle_faults
from app.samsara.safety_parser import parse_safety_event
from tests.test_safety_fixes import FakeBot, _raw_event

FIXTURE = Path("tests/fixtures/samsara/fault_feed_initial.json")


async def _setup(tmp_path, routes):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'prod.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="fleet", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        for chat_id, thread_id, purpose in routes:
            session.add(TelegramRoute(samsara_account_id=account.id, chat_id=chat_id, thread_id=thread_id,
                                      purpose=purpose, active=True, created_at=now, updated_at=now))
        await session.flush()
        safety = parse_safety_event(_raw_event("s1", label="MobileUsage"))
        await process_safety_event(session, safety, initialization=False, send_initial=False,
                                   timezone="America/New_York", integration_id=account.id,
                                   notification_delay_seconds=0)
        vehicle = json.loads(FIXTURE.read_text())["data"][0]
        fault = parse_vehicle_faults(vehicle)[0]
        fault = replace(fault, vehicle_id=f"{account.id}:{fault.vehicle_id}")
        await process_fault(session, fault, initialization=False, send_initial=False, send_cleared=True,
                            reopen_window_hours=24, timezone="America/New_York", integration_id=account.id)
    return sessions


async def _deliver(sessions):
    bot = FakeBot()
    worker = OutboxWorker(Settings(token_encryption_key=Fernet.generate_key().decode()), sessions, bot)
    for _ in range(3):
        await worker.send_pending_once()
    delivered = sorted(
        (c[1]["chat_id"], c[1]["message_thread_id"], "fault" if "FLT-" in c[1]["text"] else "safety")
        for c in bot.calls
    )
    async with sessions() as session:
        failed = [r for r in (await session.scalars(select(NotificationOutbox))).all()
                  if r.status not in ("sent", "dead")]
    return delivered, failed


@pytest.mark.parametrize("purpose, expected", [
    ("all", ["fault", "safety"]),
    ("both", ["fault", "safety"]),
    ("safety", ["safety"]),
    ("fault", ["fault"]),
    ("fuel", []),
])
async def test_plain_group_without_topics(tmp_path, purpose, expected) -> None:
    sessions = await _setup(tmp_path, [(-100, None, purpose)])
    delivered, failed = await _deliver(sessions)
    assert delivered == [(-100, None, kind) for kind in expected]
    assert not failed


async def test_single_topic_activation_keeps_working(tmp_path) -> None:
    sessions = await _setup(tmp_path, [(-200, 55, "all")])
    delivered, failed = await _deliver(sessions)
    assert delivered == [(-200, 55, "fault"), (-200, 55, "safety")]
    assert not failed


async def test_mixed_production_setup(tmp_path) -> None:
    """One fleet in a plain group, a second group split by old /activate topics,
    and a third group using the new per-type topics."""
    sessions = await _setup(tmp_path, [
        (-100, None, "both"),
        (-200, 11, "fault"),
        (-200, 12, "safety"),
        (-300, 21, "fault"),
        (-300, 22, "s:distraction"),
        (-300, 23, "s:speeding"),
    ])
    delivered, failed = await _deliver(sessions)
    assert delivered == [
        (-300, 21, "fault"),
        (-300, 22, "safety"),
        (-200, 11, "fault"),
        (-200, 12, "safety"),
        (-100, None, "fault"),
        (-100, None, "safety"),
    ]
    assert not failed
