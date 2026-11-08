from __future__ import annotations

import argparse
import asyncio
import getpass
import platform
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from sqlalchemy import func, select

from app.bot.handlers import build_router
from app.bot.admin_handlers import build_admin_router
from app.bot.safety_handlers import build_safety_router
from app.config import Settings, get_settings
from app.database.models import IntegrationState, NotificationOutbox
from app.database.session import create_database, create_tables
from app.logging_config import configure_logging
from app.notifications.worker import OutboxWorker
from app.samsara.client import MockSamsaraClient, SamsaraClient
from app.workers.fault_monitor import CURSOR_KEY, FaultMonitor
from app.workers.locking import ProcessLock
from app.workers.integration_manager import IntegrationManager
from app.workers.safety_monitor import CURSOR_KEY as SAFETY_CURSOR_KEY, SafetyMonitor


def samsara_client(settings: Settings) -> SamsaraClient | MockSamsaraClient:
    if settings.samsara_mock_mode:
        return MockSamsaraClient(settings.samsara_mock_fixture)
    return SamsaraClient(
        settings.samsara_api_token,
        settings.samsara_base_url,
        settings.samsara_request_timeout_seconds,
        settings.samsara_max_retries,
    )


async def doctor(settings: Settings) -> int:
    engine, sessions = create_database(settings.database_url)
    results: list[tuple[str, str]] = [
        ("Python", platform.python_version()),
        ("Configuration", "OK"),
        ("Telegram token", "configured" if settings.bot_token else "missing"),
        ("Telegram destination", "configured" if settings.telegram_fault_group_id else "missing"),
        ("Samsara mode", "mock" if settings.samsara_mock_mode else "live"),
        ("Samsara token", "configured" if settings.samsara_api_token else "missing"),
    ]
    try:
        await create_tables(engine)
        async with sessions() as session:
            cursor = await session.get(IntegrationState, CURSOR_KEY)
            safety_cursor = await session.get(IntegrationState, SAFETY_CURSOR_KEY)
            pending = await session.scalar(
                select(func.count()).select_from(NotificationOutbox).where(
                    NotificationOutbox.status == "pending"
                )
            )
            failed = await session.scalar(
                select(func.count()).select_from(NotificationOutbox).where(
                    NotificationOutbox.status.in_(("failed", "dead"))
                )
            )
        results.extend(
            [
                ("Database", "OK"),
                ("Saved cursor", "present" if cursor and cursor.value else "not initialized"),
                ("Saved safety cursor", "present" if safety_cursor and safety_cursor.value else "not initialized"),
                ("Pending notifications", str(pending or 0)),
                ("Failed notifications", str(failed or 0)),
            ]
        )
    except Exception as exc:
        results.append(("Database", f"ERROR: {type(exc).__name__}"))
    if settings.bot_token:
        try:
            bot = Bot(settings.bot_token)
            me = await bot.get_me()
            results.append(("Telegram API", f"OK (@{me.username})"))
            await bot.session.close()
        except Exception as exc:
            results.append(("Telegram API", f"ERROR: {type(exc).__name__}"))
    client = samsara_client(settings)
    try:
        page = await client.get_fault_code_feed(None)
        results.append(("Fault feed", f"OK ({len(page.data)} vehicle records)"))
        start = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        safety_page = await client.get_safety_event_stream(start_time=start, end_time=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        results.append(("Safety stream", f"OK ({len(safety_page.data)} recent events)"))
    except Exception as exc:
        results.append(("Fault feed", f"ERROR: {type(exc).__name__}"))
    finally:
        await client.close()
        await engine.dispose()
    width = max(len(name) for name, _ in results)
    for name, value in results:
        print(f"{name:<{width}}  {value}")
    return 0


async def poll_once(settings: Settings, send: bool = False) -> int:
    settings.require_monitor()
    engine, sessions = create_database(settings.database_url)
    await create_tables(engine)
    client = samsara_client(settings)
    monitor = FaultMonitor(settings, sessions, client)
    try:
        totals = await monitor.poll_once()
        print("Poll complete:", " ".join(f"{key}={value}" for key, value in totals.items()))
        if send:
            settings.require_bot()
            bot = Bot(settings.bot_token)
            worker = OutboxWorker(settings, sessions, bot)
            count = await worker.send_pending_once()
            print(f"Notifications sent: {count}")
            await bot.session.close()
    finally:
        await client.close()
        await engine.dispose()
    return 0


async def poll_safety_once(settings: Settings, send: bool = False) -> int:
    settings.require_monitor()
    engine, sessions = create_database(settings.database_url)
    await create_tables(engine)
    client = samsara_client(settings)
    monitor = SafetyMonitor(settings, sessions, client)
    try:
        totals = await monitor.poll_once()
        print("Safety poll complete:", " ".join(f"{key}={value}" for key, value in totals.items()))
        if send:
            settings.require_bot()
            bot = Bot(settings.bot_token)
            count = await OutboxWorker(settings, sessions, bot).send_pending_once()
            print(f"Notifications sent: {count}")
            await bot.session.close()
    finally:
        await client.close()
        await engine.dispose()
    return 0


async def run(settings: Settings, monitor_only: bool = False) -> int:
    settings.require_bot()
    engine, sessions = create_database(settings.database_url)
    await create_tables(engine)
    lock = ProcessLock(settings.worker_lock_name)
    lock.acquire()
    manager = IntegrationManager(settings, sessions)
    tasks = [asyncio.create_task(manager.run())]
    bot = None
    outbox = None
    if not monitor_only:
        settings.require_bot()
        bot = Bot(settings.bot_token)
        dp = Dispatcher()
        dp.include_router(build_admin_router(settings, sessions))
        dp.include_router(build_router(settings, sessions))
        dp.include_router(build_safety_router(settings, sessions))
        outbox = OutboxWorker(settings, sessions, bot)
        tasks.extend([asyncio.create_task(outbox.run()), asyncio.create_task(dp.start_polling(bot))])
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        manager.stop()
        if outbox:
            outbox.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot:
            await bot.session.close()
        await engine.dispose()
        lock.release()
    return 0


async def cursor_command(settings: Settings, reset: bool) -> int:
    engine, sessions = create_database(settings.database_url)
    await create_tables(engine)
    async with sessions.begin() as session:
        row = await session.get(IntegrationState, CURSOR_KEY)
        if not reset:
            print(row.value if row and row.value else "(no cursor)")
        elif row:
            confirmation = input("Type RESET to delete the saved cursor: ")
            if confirmation != "RESET":
                print("Cancelled.")
            else:
                await session.delete(row)
                print("Cursor reset. The next poll will perform first-run baseline processing.")
    await engine.dispose()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Samsara Telegram fault monitor")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "bot", "monitor", "poll-once", "poll-faults-once", "poll-safety-once", "doctor", "show-cursor", "reset-cursor"),
    )
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 2
    args = parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file)
    if args.command == "doctor":
        return asyncio.run(doctor(settings))
    if args.command in {"poll-once", "poll-faults-once"}:
        return asyncio.run(poll_once(settings))
    if args.command == "poll-safety-once":
        return asyncio.run(poll_safety_once(settings))
    if args.command == "show-cursor":
        return asyncio.run(cursor_command(settings, False))
    if args.command == "reset-cursor":
        return asyncio.run(cursor_command(settings, True))
    try:
        return asyncio.run(run(settings, monitor_only=args.command == "monitor"))
    except RuntimeError as exc:
        if "worker lock" in str(exc).lower():
            print(str(exc), file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
