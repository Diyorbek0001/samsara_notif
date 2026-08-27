from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.faults.service import state_get, state_set
from app.safety.service import process_safety_event
from app.samsara.client import MockSamsaraClient, SamsaraClient
from app.samsara.safety_parser import parse_safety_event

logger = logging.getLogger(__name__)
CURSOR_KEY = "samsara_safety_cursor"
START_KEY = "samsara_safety_start_time"
LAST_SUCCESS_KEY = "last_successful_safety_poll"
SAFETY_OVERLAP = timedelta(minutes=30)


class SafetyMonitor:
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

    async def _obstruction_snapshot(self, parsed):
        labels = {label.replace(" ", "").lower() for label in parsed.behaviors}
        if parsed.media or not parsed.vehicle_id or not (
            labels & {"obstructedcamera", "cameraobstruction"}
        ):
            return parsed
        key = f"safety_snapshot_requested:{parsed.samsara_event_id}"
        async with self.sessions.begin() as session:
            if await state_get(session, key):
                return parsed
            await state_set(session, key, datetime.now(UTC).isoformat())
        try:
            media = await self.client.retrieve_camera_snapshots(
                parsed.vehicle_id, parsed.event_timestamp
            )
        except Exception:
            logger.exception(
                "safety_snapshot_retrieval_failed event_id=%s",
                parsed.samsara_event_id,
            )
            return parsed
        logger.info(
            "safety_snapshot_retrieved event_id=%s media_urls=%d",
            parsed.samsara_event_id,
            len(media),
        )
        return replace(parsed, media=media) if media else parsed

    async def _speed_enrichment(self, parsed):
        labels = {label.replace(" ", "").lower() for label in parsed.behaviors}
        if not parsed.vehicle_id or not any("speed" in label for label in labels):
            return parsed
        key = f"safety_speed_details:{parsed.samsara_event_id}"
        async with self.sessions() as session:
            cached = await state_get(session, key)
        details = None
        if cached:
            try:
                stored = json.loads(cached)
                if isinstance(stored, dict) and stored.get("details"):
                    details = stored["details"]
                elif isinstance(stored, dict) and stored.get("checkedAt"):
                    checked = datetime.fromisoformat(stored["checkedAt"])
                    if datetime.now(UTC) - checked < timedelta(minutes=5):
                        return parsed
            except (ValueError, TypeError):
                pass
        if details is None:
            try:
                details = await self.client.get_speeding_details(
                    parsed.vehicle_id, parsed.event_timestamp
                )
            except Exception:
                logger.exception(
                    "safety_speed_enrichment_failed event_id=%s",
                    parsed.samsara_event_id,
                )
                async with self.sessions.begin() as session:
                    await state_set(
                        session,
                        key,
                        json.dumps({"checkedAt": datetime.now(UTC).isoformat()}),
                    )
                return parsed
            stored = {
                "checkedAt": datetime.now(UTC).isoformat(),
                "details": details,
            }
            async with self.sessions.begin() as session:
                await state_set(session, key, json.dumps(stored))
        if not details:
            return parsed
        raw = dict(parsed.raw)
        raw["speedingDetails"] = details
        return replace(parsed, raw=raw)

    async def _speed_video(self, parsed):
        labels = {label.replace(" ", "").lower() for label in parsed.behaviors}
        if parsed.media or not parsed.vehicle_id or not any(
            "speed" in label for label in labels
        ):
            return parsed
        key = f"safety_speed_video_requested:{parsed.samsara_event_id}"
        async with self.sessions.begin() as session:
            if await state_get(session, key):
                return parsed
            await state_set(session, key, datetime.now(UTC).isoformat())
        try:
            media = await self.client.retrieve_camera_video(
                parsed.vehicle_id, parsed.event_timestamp
            )
        except Exception:
            logger.exception(
                "safety_speed_video_retrieval_failed event_id=%s",
                parsed.samsara_event_id,
            )
            return parsed
        logger.info(
            "safety_speed_video_retrieved event_id=%s media_urls=%d",
            parsed.samsara_event_id,
            len(media),
        )
        return replace(parsed, media=media) if media else parsed

    async def poll_once(self) -> dict[str, int]:
        totals = {"pages": 0, "records": 0, "created": 0, "updated": 0}
        async with self._cycle_lock:
            cycle_end = datetime.now(UTC)
            async with self.sessions() as session:
                cursor = await state_get(session, CURSOR_KEY)
                suffix = f":{self.integration_id}" if self.integration_id else ""
                last_success = await state_get(session, LAST_SUCCESS_KEY + suffix)
            initialization = last_success is None
            if last_success:
                try:
                    last_poll = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
                    window_start = last_poll - SAFETY_OVERLAP
                except ValueError:
                    window_start = cycle_end - timedelta(
                        minutes=self.settings.safety_initial_lookback_minutes
                    )
            else:
                window_start = cycle_end - timedelta(
                    minutes=self.settings.safety_initial_lookback_minutes
                )
            start = window_start.isoformat().replace("+00:00", "Z")
            end = cycle_end.isoformat().replace("+00:00", "Z")
            # Cursors are only valid within this bounded pagination pass. Starting
            # each cycle from the last successful time makes downtime recoverable.
            after = None
            while True:
                media_urls = 0
                page = await self.client.get_safety_event_stream(
                    start_time=start,
                    end_time=end,
                    after=after,
                    # Samsara often attaches camera media after first creating the
                    # safety event. Poll updates so those later URLs are delivered.
                    query_by_time_field="updatedAtTime",
                )
                async with self.sessions.begin() as session:
                    for raw in page.data:
                        parsed = parse_safety_event(raw)
                        if not parsed:
                            continue
                        parsed = await self._obstruction_snapshot(parsed)
                        parsed = await self._speed_enrichment(parsed)
                        parsed = await self._speed_video(parsed)
                        media_urls += sum(
                            1 for item in parsed.media if item.get("url")
                        )
                        if self.integration_id:
                            parsed = replace(
                                parsed,
                                samsara_event_id=f"{self.integration_id}:{parsed.samsara_event_id}",
                                vehicle_id=f"{self.integration_id}:{parsed.vehicle_id}" if parsed.vehicle_id else None,
                                driver_id=f"{self.integration_id}:{parsed.driver_id}" if parsed.driver_id else None,
                            )
                        names = {name.lower() for name in parsed.behaviors}
                        if self.settings.allowed_safety_behaviors and not (
                            names & self.settings.allowed_safety_behaviors
                        ):
                            continue
                        if names & self.settings.ignored_safety_behaviors:
                            continue
                        _, result = await process_safety_event(
                            session,
                            parsed,
                            initialization=initialization,
                            send_initial=self.settings.send_initial_safety_events,
                            timezone=self.settings.timezone,
                            integration_id=self.integration_id,
                            notification_delay_seconds=(
                                self.settings.safety_notification_delay_seconds
                            ),
                        )
                        totals["records"] += 1
                        if result in totals:
                            totals[result] += 1
                    await state_set(session, START_KEY, start)
                    await state_set(session, CURSOR_KEY, page.end_cursor)
                    await state_set(session, LAST_SUCCESS_KEY + suffix, cycle_end.isoformat())
                totals["pages"] += 1
                logger.info(
                    "safety_cursor_committed cursor_hash=%s page=%d raw_records=%d "
                    "records=%d created=%d updated=%d media_urls=%d",
                    hashlib.sha256(page.end_cursor.encode()).hexdigest()[:10],
                    totals["pages"],
                    len(page.data),
                    totals["records"],
                    totals["created"],
                    totals["updated"],
                    media_urls,
                )
                after = page.end_cursor
                initialization = False
                if not page.has_next_page:
                    return totals

    async def run(self) -> None:
        logger.info("safety_monitor_started")
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                logger.exception("safety_monitor_cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.safety_poll_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
