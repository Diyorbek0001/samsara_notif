from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.bot import fuel_handlers
from app.bot.fuel_handlers import (
    build_fuel_router,
    fuel_snapshot_message,
    parse_fuel_snapshot,
)
from app.config import Settings
from app.database.models import SamsaraAccount, TelegramRoute
from app.database.session import create_database, create_tables
from app.security import TokenCipher


def test_fuel_snapshot_sorts_low_to_high_and_missing_last() -> None:
    rows = parse_fuel_snapshot(
        [
            {
                "name": "Truck 30",
                "gps": [{"latitude": 40.1, "longitude": -74.2}],
                "fuelPercents": [{"value": 80}],
            },
            {
                "name": "Truck 10",
                "gps": [{"latitude": 41.1, "longitude": -75.2}],
                "fuelPercents": [{"value": 25}],
            },
            {
                "name": "Truck 20",
                "gps": [{"latitude": 42.1, "longitude": -76.2}],
                "fuelPercents": [],
            },
        ]
    )

    assert [row.unit_name for row in rows] == ["Truck 10", "Truck 30", "Truck 20"]


def test_fuel_snapshot_message_lists_coordinates_and_fuel() -> None:
    rows = parse_fuel_snapshot(
        [
            {
                "name": "Truck <1>",
                "gps": [{"latitude": "40.1234567", "longitude": "-74.7654321"}],
                "fuelPercents": [{"value": "12.5"}],
            }
        ]
    )

    text = fuel_snapshot_message(rows)

    assert "Truck &lt;1&gt;" in text
    assert "12.5%" in text
    assert "40.123457, -74.765432" in text


def test_fuel_snapshot_accepts_singular_and_nested_fuel_fields() -> None:
    rows = parse_fuel_snapshot(
        [
            {
                "name": "Truck 40",
                "gps": {"lat": "40.1", "lng": "-74.2"},
                "fuelPercent": {"time": "2026-09-05T00:00:00Z", "value": {"percent": 44}},
            },
            {
                "name": "Truck 50",
                "gps": {"latitude": 41.1, "longitude": -75.2},
                "fuelLevelPercent": [{"time": "2026-09-05T00:00:00Z", "fuelPercentage": "9"}],
            },
        ]
    )

    assert [(row.unit_name, row.fuel_percent) for row in rows] == [
        ("Truck 50", 9.0),
        ("Truck 40", 44.0),
    ]


@pytest.mark.asyncio
async def test_currentfuel_allows_any_group_member_in_activated_fuel_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = Settings(
        token_encryption_key=Fernet.generate_key().decode(),
        admin_telegram_ids="111",
        support_telegram_ids="222",
        maintenance_team_telegram_ids="333",
    )
    cipher = TokenCipher(settings.token_encryption_key)
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'fuel.db'}")
    await create_tables(engine)

    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(
            name="Main",
            encrypted_api_token=cipher.encrypt("samsara-secret"),
            active=True,
            created_by_telegram_id=111,
            created_at=now,
            updated_at=now,
        )
        session.add(account)
        await session.flush()
        session.add(
            TelegramRoute(
                samsara_account_id=account.id,
                chat_id=-100123,
                thread_id=55,
                chat_title="Fuel Level",
                purpose="fuel",
                active=True,
                created_by_telegram_id=111,
                created_at=now,
                updated_at=now,
            )
        )

    calls: list[str] = []

    class FakeSamsaraClient:
        def __init__(self, token, *_args):
            calls.append(token)

        async def get_current_fuel_snapshot(self):
            return [
                {
                    "name": "Truck 10",
                    "gps": [{"latitude": 40.1, "longitude": -74.2}],
                    "fuelPercents": [{"value": 25}],
                }
            ]

        async def close(self):
            pass

    monkeypatch.setattr(fuel_handlers, "SamsaraClient", FakeSamsaraClient)
    router = build_fuel_router(settings, sessions)
    handler = router.message.handlers[0].callback
    replies: list[tuple[str, str | None]] = []

    class FakeMessage(SimpleNamespace):
        async def answer(self, text, parse_mode=None):
            replies.append((text, parse_mode))

    message = FakeMessage(
        from_user=SimpleNamespace(id=999999, username="ordinary_member"),
        chat=SimpleNamespace(id=-100123, type="supergroup"),
        message_thread_id=55,
    )

    await handler(message)
    await engine.dispose()

    assert calls == ["samsara-secret"]
    assert replies
    assert "Truck 10" in replies[0][0]
    assert replies[0][1] == "HTML"
