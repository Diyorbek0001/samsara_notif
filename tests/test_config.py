from app.config import Settings


def test_id_lists_are_parsed_safely():
    settings = Settings(
        _env_file=None,
        admin_telegram_ids="1, 2",
        support_telegram_ids="3",
    )
    assert settings.authorized_ids == frozenset({1, 2, 3})


def test_id_lists_load_from_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "10, 20")
    settings = Settings(_env_file=None)
    assert settings.authorized_ids == frozenset({10, 20})


def test_topic_is_optional():
    settings = Settings(_env_file=None, telegram_fault_topic_id="")
    assert settings.telegram_fault_topic_id is None


def test_admins_can_be_ids_or_usernames():
    settings = Settings(
        _env_file=None,
        admin_telegram_ids="7513839793, FleetAdmin",
    )
    assert settings.authorized_ids == frozenset({7513839793})
    assert settings.authorized_usernames == frozenset({"fleetadmin"})
    assert settings.is_authorized(None, "FleetAdmin")
    assert settings.is_authorized(7513839793, None)
    assert settings.is_admin(None, "fleetadmin")


def test_railway_postgres_url_uses_async_driver():
    settings = Settings(_env_file=None, database_url="postgresql://user:pass@db/app")
    assert settings.database_url == "postgresql+asyncpg://user:pass@db/app"
