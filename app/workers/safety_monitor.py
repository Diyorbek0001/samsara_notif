from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import SafetyEvent
from app.faults.service import state_get, state_set
from app.safety.service import process_safety_event, queue_speed_update
from app.samsara.client import MockSamsaraClient, SamsaraClient
from app.samsara.exceptions import SamsaraAuthenticationError, SamsaraPermissionError
from app.samsara.safety_parser import parse_safety_event

logger = logging.getLogger(__name__)
CURSOR_KEY = "samsara_safety_cursor"
START_KEY = "samsara_safety_start_time"
LAST_SUCCESS_KEY = "last_successful_safety_poll"
INITIAL_START_KEY = "samsara_safety_initial_start"
SAFETY_OVERLAP = timedelta(minutes=30)
# Samsara publishes speeding intervals (with the posted limit) only after the
# trip ends, so keep re-checking recent speeding events for a while.
SPEED_RECHECK_INTERVAL = timedelta(minutes=5)
SPEED_RECHECK_MAX_AGE = timedelta(hours=12)
MEDIA_PERMISSION_BACKOFF = timedelta(hours=1)
SNAPSHOT_RECHECK_INTERVAL = timedelta(seconds=30)
SNAPSHOT_LOOKBACK = timedelta(minutes=10)


def _is_speeding(behaviors: list[str]) -> bool:
    return any("speed" in label.replace(" ", "").lower() for label in behaviors)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class SafetyMonitor:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        client: SamsaraClient | MockSamsaraClient,
        integration_id: int | None = None,
        write_lock: asyncio.Lock | None = None,
    ):
        self.settings, self.sessions, self.client = settings, sessions, client
        self.integration_id = integration_id
        # Shared with this account's FaultMonitor so both never write vehicle
        # rows at the same time (concurrent inserts deadlocked in PostgreSQL).
        self.write_lock = write_lock or asyncio.Lock()
        self._stop = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._media_blocked_until: datetime | None = None
        self._last_speed_recheck: datetime | None = None
        self._suffix = f":{integration_id}" if integration_id else ""

    def _media_allowed(self) -> bool:
        return self._media_blocked_until is None or datetime.now(UTC) >= self._media_blocked_until

    def _media_failed(self, exc: Exception, event_id: str) -> None:
        if isinstance(exc, (SamsaraAuthenticationError, SamsaraPermissionError)):
            # Read-only tokens get 401/403 here. Log once and pause instead of
            # failing (with a traceback) on every camera event.
            self._media_blocked_until = datetime.now(UTC) + MEDIA_PERMISSION_BACKOFF
            logger.warning(
                "camera_media_retrieval_unauthorized integration_id=%s "
                "hint=the Samsara API token needs Read and Write Media Retrieval "
                "paused_minutes=%d",
                self.integration_id,
                MEDIA_PERMISSION_BACKOFF.total_seconds() // 60,
            )
            return
        logger.exception("safety_media_retrieval_failed event_id=%s", event_id)

    @staticmethod
    def _is_obstruction(behaviors: list[str]) -> bool:
        labels = {label.replace(" ", "").lower() for label in behaviors}
        return bool(labels & {"obstructedcamera", "cameraobstruction"})

    @staticmethod
    def _pick_still(stills: list[dict], event_time: datetime) -> tuple[list[dict], bool]:
        """Pick the dashcam stills that best show the camera at event time.

        Prefers the first capture at/after the event (it shows the obstruction),
        otherwise the latest capture before it. Returns all camera inputs from
        that capture (road + driver) and whether it was taken after the event.
        """
        by_time: dict[datetime, list[dict]] = {}
        for still in stills:
            captured = _parse_time(still.get("capturedAt"))
            if captured:
                by_time.setdefault(captured, []).append(still)
        if not by_time:
            return [], False
        tolerance = timedelta(seconds=30)
        after = sorted(t for t in by_time if t >= event_time - tolerance)
        if after:
            chosen, is_after = after[0], True
        else:
            chosen, is_after = max(by_time), False
        order = {"dashcamRoadFacing": 0, "dashcamDriverFacing": 1}
        group = sorted(by_time[chosen], key=lambda item: order.get(item["input"], 2))
        return group, is_after

    async def _obstruction_snapshot(self, parsed):
        """Attach the dashcam still showing the obstructed camera.

        Returns (parsed, found_after_event). Uses stills Samsara already
        uploaded, which only needs read access. Checks again on later polls
        until a still taken after the event shows up.
        """
        if not parsed.vehicle_id or not self._is_obstruction(parsed.behaviors):
            return parsed, False
        if any(item.get("url") for item in parsed.media):
            return parsed, True
        key = f"safety_snapshot:{parsed.samsara_event_id}"
        async with self.sessions() as session:
            cached = await state_get(session, key)
        stored = {}
        if cached:
            try:
                stored = json.loads(cached) or {}
            except (ValueError, TypeError):
                stored = {}
        now = datetime.now(UTC)
        checked = _parse_time(stored.get("checkedAt"))
        if not stored.get("afterEvent") and (
            not checked or now - checked >= SNAPSHOT_RECHECK_INTERVAL
        ):
            try:
                stills = await self.client.get_camera_stills(
                    parsed.vehicle_id,
                    parsed.event_timestamp - SNAPSHOT_LOOKBACK,
                    min(
                        now,
                        parsed.event_timestamp
                        + timedelta(seconds=self.settings.safety_snapshot_max_wait_seconds)
                        + SNAPSHOT_LOOKBACK,
                    ),
                )
            except Exception:
                logger.exception(
                    "safety_snapshot_lookup_failed event_id=%s", parsed.samsara_event_id
                )
                stills = []
            media, is_after = self._pick_still(stills, parsed.event_timestamp)
            if media:
                stored = {"media": media, "afterEvent": is_after}
                if is_after:
                    logger.info(
                        "safety_snapshot_found event_id=%s captured_at=%s inputs=%d",
                        parsed.samsara_event_id,
                        media[0]["capturedAt"],
                        len(media),
                    )
            stored["checkedAt"] = now.isoformat()
            async with self.sessions.begin() as session:
                await state_set(session, key, json.dumps(stored))
        media = stored.get("media") or []
        if not media:
            return parsed, False
        raw = dict(parsed.raw)
        raw["snapshotInfo"] = {
            "capturedAt": media[0]["capturedAt"],
            "afterEvent": bool(stored.get("afterEvent")),
        }
        clean = [
            {"input": item["input"], "url": item["url"], "mediaType": "image"}
            for item in media
        ]
        return replace(parsed, media=clean, raw=raw), bool(stored.get("afterEvent"))

    async def _speeding_details(
        self,
        event_id: str,
        vehicle_id: str,
        event_time: datetime,
        trip_start: datetime | None,
    ) -> dict | None:
        """Return cached or freshly fetched speeding details for one event.

        Details are final once they include the posted speed limit. Until then
        Samsara is asked again at most every SPEED_RECHECK_INTERVAL.
        """
        key = f"safety_speed_details:{event_id}"
        async with self.sessions() as session:
            cached = await state_get(session, key)
        details = None
        if cached:
            try:
                stored = json.loads(cached)
            except (ValueError, TypeError):
                stored = None
            if isinstance(stored, dict):
                details = stored.get("details") or None
                if details and details.get("postedSpeedLimitMph") is not None:
                    return details
                checked = _parse_time(stored.get("checkedAt"))
                if checked and datetime.now(UTC) - checked < SPEED_RECHECK_INTERVAL:
                    return details
        try:
            fetched = await self.client.get_speeding_details(
                vehicle_id, event_time, trip_start
            )
        except Exception:
            logger.exception("safety_speed_enrichment_failed event_id=%s", event_id)
            fetched = None
        # Keep earlier partial details (max speed) if the new lookup found less.
        if fetched and (
            not details
            or fetched.get("postedSpeedLimitMph") is not None
            or details.get("postedSpeedLimitMph") is None
        ):
            details = fetched
        async with self.sessions.begin() as session:
            await state_set(
                session,
                key,
                json.dumps({"checkedAt": datetime.now(UTC).isoformat(), "details": details}),
            )
        return details

    async def _speed_enrichment(self, parsed):
        if not parsed.vehicle_id or not _is_speeding(parsed.behaviors):
            return parsed
        details = await self._speeding_details(
            parsed.samsara_event_id,
            parsed.vehicle_id,
            parsed.event_timestamp,
            _parse_time(parsed.raw.get("tripStartTime")),
        )
        if not details:
            return parsed
        raw = dict(parsed.raw)
        raw["speedingDetails"] = details
        return replace(parsed, raw=raw)

    async def _speed_video(self, parsed):
        if (
            any(item.get("url") for item in parsed.media)
            or not parsed.vehicle_id
            or not _is_speeding(parsed.behaviors)
        ):
            return parsed
        if not self._media_allowed():
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
        except Exception as exc:
            self._media_failed(exc, parsed.samsara_event_id)
            if not self._media_allowed():
                # Permission problem, not this event: allow a retry once the
                # token is fixed and the pause ends.
                async with self.sessions.begin() as session:
                    await state_set(session, key, "")
            return parsed
        logger.info(
            "safety_speed_video_retrieved event_id=%s media_urls=%d",
            parsed.samsara_event_id,
            len(media),
        )
        return replace(parsed, media=media) if media else parsed

    async def recheck_speed_limits(self) -> int:
        """Fill in posted speed limits for recent speeding events.

        Events leave the live polling window after SAFETY_OVERLAP, but Samsara
        only publishes the posted limit when the trip ends, which can be hours
        later. This sweep looks those events up again and queues an update.
        """
        now = datetime.now(UTC)
        if self._last_speed_recheck and now - self._last_speed_recheck < SPEED_RECHECK_INTERVAL:
            return 0
        self._last_speed_recheck = now
        prefix = f"{self.integration_id}:" if self.integration_id else ""
        async with self.sessions() as session:
            query = select(SafetyEvent).where(
                SafetyEvent.created_at >= now - SPEED_RECHECK_MAX_AGE,
                SafetyEvent.primary_behavior.ilike("%speed%"),
                # Baseline events were stored without alerting; don't alert now.
                SafetyEvent.initialized_baseline.is_(False),
            )
            if prefix:
                query = query.where(SafetyEvent.samsara_event_id.startswith(prefix))
            rows = list((await session.scalars(query)).all())
        updated = 0
        for row in rows:
            raw = row.raw_event_json if isinstance(row.raw_event_json, dict) else {}
            current = raw.get("speedingDetails")
            if isinstance(current, dict) and current.get("postedSpeedLimitMph") is not None:
                continue
            asset = raw.get("asset") if isinstance(raw.get("asset"), dict) else {}
            vehicle_id = asset.get("id")
            if not vehicle_id:
                continue
            raw_event_id = row.samsara_event_id.removeprefix(prefix)
            event_time = row.event_timestamp
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=UTC)
            details = await self._speeding_details(
                raw_event_id,
                str(vehicle_id),
                event_time,
                _parse_time(raw.get("tripStartTime")),
            )
            if not details or details.get("postedSpeedLimitMph") is None:
                continue
            async with self.write_lock, self.sessions.begin() as session:
                db_row = await session.get(SafetyEvent, row.id)
                db_raw = dict(db_row.raw_event_json or {})
                db_raw["speedingDetails"] = details
                db_row.raw_event_json = db_raw
                db_row.updated_at = datetime.now(UTC)
                await queue_speed_update(session, db_row, details, self.integration_id)
            updated += 1
            logger.info("safety_speed_limit_backfilled alert_id=%s", row.alert_id)
        return updated

    async def poll_once(self) -> dict[str, int]:
        totals = {"pages": 0, "records": 0, "created": 0, "updated": 0}
        suffix = self._suffix
        async with self._cycle_lock:
            cycle_end = datetime.now(UTC)
            async with self.sessions() as session:
                last_success = await state_get(session, LAST_SUCCESS_KEY + suffix)
                initial_start = _parse_time(
                    await state_get(session, INITIAL_START_KEY + suffix)
                )
            initialization = last_success is None
            window_start = cycle_end - timedelta(
                minutes=self.settings.safety_initial_lookback_minutes
            )
            if last_success:
                last_poll = _parse_time(last_success)
                if last_poll:
                    window_start = last_poll - SAFETY_OVERLAP
                    # Never look further back than the first (baseline) poll.
                    # Otherwise the second poll treats events from before the
                    # baseline as new and alerts on them.
                    if initial_start and window_start < initial_start:
                        window_start = initial_start
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
                # Enrich first (network calls), then write everything in one
                # short transaction.
                events = []
                for raw in page.data:
                    parsed = parse_safety_event(raw)
                    if not parsed:
                        continue
                    parsed, snapshot_ready = await self._obstruction_snapshot(parsed)
                    delay = self.settings.safety_notification_delay_seconds
                    if self._is_obstruction(parsed.behaviors):
                        # Hold the alert until the post-event still arrives,
                        # at most safety_snapshot_max_wait_seconds after the event.
                        deadline = parsed.event_timestamp + timedelta(
                            seconds=self.settings.safety_snapshot_max_wait_seconds
                        )
                        delay = 0 if snapshot_ready else max(
                            0, int((deadline - datetime.now(UTC)).total_seconds())
                        )
                    parsed = await self._speed_enrichment(parsed)
                    parsed = await self._speed_video(parsed)
                    media_urls += sum(1 for item in parsed.media if item.get("url"))
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
                    events.append((parsed, delay, snapshot_ready))
                async with self.write_lock, self.sessions.begin() as session:
                    for parsed, delay, snapshot_ready in events:
                        _, result = await process_safety_event(
                            session,
                            parsed,
                            initialization=initialization,
                            send_initial=self.settings.send_initial_safety_events,
                            timezone=self.settings.timezone,
                            integration_id=self.integration_id,
                            notification_delay_seconds=delay,
                            release_pending=snapshot_ready,
                        )
                        totals["records"] += 1
                        if result in totals:
                            totals[result] += 1
                    if initialization and totals["pages"] == 0:
                        await state_set(session, INITIAL_START_KEY + suffix, start)
                    await state_set(session, START_KEY + suffix, start)
                    await state_set(session, CURSOR_KEY + suffix, page.end_cursor)
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
                    break
        try:
            await self.recheck_speed_limits()
        except Exception:
            logger.exception("safety_speed_recheck_failed")
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
