from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.faults.service import process_fault, state_get, state_set
from app.samsara.client import MockSamsaraClient, SamsaraClient
from app.samsara.parsers import parse_vehicle_faults

logger = logging.getLogger(__name__)
CURSOR_KEY = "samsara_fault_feed_cursor"


class FaultMonitor:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        client: SamsaraClient | MockSamsaraClient,
        integration_id: int | None = None,
    ):
        self.settings, self.sessions, self.client = settings, sessions, client
        self.integration_id = integration_id
        self._stop = asyncio.Event()
        self._cycle_lock = asyncio.Lock()

    async def poll_once(self) -> dict[str, int]:
        if self._cycle_lock.locked():
            raise RuntimeError("A fault polling cycle is already running in this process")
        totals = {"pages": 0, "records": 0, "created": 0, "updated": 0, "cleared": 0}
        async with self._cycle_lock:
            async with self.sessions() as session:
                cursor = await state_get(session, f"{CURSOR_KEY}:{self.integration_id}" if self.integration_id else CURSOR_KEY)
            initialization = cursor is None
            after = cursor
            while True:
                page = await self.client.get_fault_code_feed(after)
                async with self.sessions.begin() as session:
                    for vehicle_payload in page.data:
                        if not isinstance(vehicle_payload, dict):
                            continue
                        faults = parse_vehicle_faults(vehicle_payload)
                        totals["records"] += len(faults)
                        for fault in faults:
                            if self.integration_id:
                                fault = replace(fault, vehicle_id=f"{self.integration_id}:{fault.vehicle_id}")
                            _, result = await process_fault(
                                session,
                                fault,
                                initialization=initialization,
                                send_initial=self.settings.send_initial_active_faults,
                                send_cleared=self.settings.send_cleared_alerts,
                                reopen_window_hours=self.settings.fault_reopen_window_hours,
                                timezone=self.settings.timezone,
                                integration_id=self.integration_id,
                            )
                            if result in totals:
                                totals[result] += 1
                    await state_set(session, f"{CURSOR_KEY}:{self.integration_id}" if self.integration_id else CURSOR_KEY, page.end_cursor)
                    await state_set(session, "last_successful_poll_at", datetime.now(UTC).isoformat())
                totals["pages"] += 1
                logger.info(
                    "cursor_committed cursor_hash=%s page=%d records=%d",
                    hashlib.sha256(page.end_cursor.encode()).hexdigest()[:10],
                    totals["pages"],
                    totals["records"],
                )
                after = page.end_cursor
                initialization = False
                if not page.has_next_page:
                    return totals

    async def run(self) -> None:
        logger.info("fault_monitor_started")
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                logger.exception("fault_monitor_cycle_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.samsara_fault_poll_seconds
                )
            except TimeoutError:
                pass
        logger.info("fault_monitor_stopped")

    def stop(self) -> None:
        self._stop.set()
