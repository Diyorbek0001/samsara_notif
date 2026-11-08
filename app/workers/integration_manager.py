from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import SamsaraAccount
from app.samsara.client import SamsaraClient
from app.security import TokenCipher
from app.workers.fault_monitor import FaultMonitor
from app.workers.safety_monitor import SafetyMonitor

logger = logging.getLogger(__name__)


class IntegrationManager:
    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]):
        self.settings, self.sessions = settings, sessions
        self.cipher = TokenCipher(settings.token_encryption_key)
        self._stop = asyncio.Event()
        self._running: dict[int, tuple[SamsaraClient, FaultMonitor, SafetyMonitor, list[asyncio.Task]]] = {}

    async def _sync(self) -> None:
        async with self.sessions() as session:
            accounts = list((await session.scalars(select(SamsaraAccount).where(SamsaraAccount.active.is_(True)))).all())
        active_ids = {row.id for row in accounts}
        for account_id in list(self._running):
            if account_id not in active_ids:
                await self._stop_account(account_id)
        for account in accounts:
            if account.id in self._running:
                continue
            client = SamsaraClient(self.cipher.decrypt(account.encrypted_api_token), self.settings.samsara_base_url, self.settings.samsara_request_timeout_seconds, self.settings.samsara_max_retries)
            write_lock = asyncio.Lock()
            fault = FaultMonitor(self.settings, self.sessions, client, account.id, write_lock)
            safety = SafetyMonitor(self.settings, self.sessions, client, account.id, write_lock)
            tasks = [asyncio.create_task(fault.run()), asyncio.create_task(safety.run())]
            self._running[account.id] = (client, fault, safety, tasks)
            logger.info("samsara_account_started account_id=%d name=%s", account.id, account.name)

    async def _stop_account(self, account_id: int) -> None:
        client, fault, safety, tasks = self._running.pop(account_id)
        fault.stop(), safety.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sync()
            except Exception:
                logger.exception("integration_manager_sync_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), 10)
            except TimeoutError:
                pass
        for account_id in list(self._running):
            await self._stop_account(account_id)

    def stop(self) -> None:
        self._stop.set()
