from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot
from aiogram.types import InputMediaPhoto, InputMediaVideo
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.formatters import cleared_message, fault_message
from app.bot.safety_formatters import safety_message
from app.config import Settings
from app.database.models import FaultIncident, NotificationLog, NotificationOutbox, SafetyEvent, TelegramRoute

logger = logging.getLogger(__name__)


class OutboxWorker:
    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession], bot: Bot):
        self.settings, self.sessions, self.bot = settings, sessions, bot
        self._stop = asyncio.Event()

    async def send_pending_once(self) -> int:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.status == "processing",
                    NotificationOutbox.processing_at < now - timedelta(minutes=10),
                )
                .values(
                    status="failed",
                    next_attempt_at=now,
                    last_error="Recovered abandoned processing claim",
                )
            )
        claimed_ids: list[int] = []
        for _ in range(20):
            async with self.sessions.begin() as session:
                candidate = (
                    select(NotificationOutbox.id)
                    .where(
                        NotificationOutbox.status.in_(("pending", "failed")),
                        NotificationOutbox.next_attempt_at <= now,
                    )
                    .order_by(NotificationOutbox.id)
                    .limit(1)
                    .scalar_subquery()
                )
                claimed_id = await session.scalar(
                    update(NotificationOutbox)
                    .where(
                        NotificationOutbox.id == candidate,
                        NotificationOutbox.status.in_(("pending", "failed")),
                    )
                    .values(status="processing", processing_at=now)
                    .returning(NotificationOutbox.id)
                )
                if claimed_id is None:
                    break
                claimed_ids.append(claimed_id)
        sent = 0
        for item_id in claimed_ids:
            await self._expand_routes(item_id)
            async with self.sessions() as session:
                item = await session.get(NotificationOutbox, item_id)
                if item.status == "dead":
                    continue
                route_id = item.payload_json.get("route_id") if item.payload_json else None
                route = await session.get(TelegramRoute, route_id) if route_id else None
                incident = await session.scalar(
                    select(FaultIncident)
                    .options(selectinload(FaultIncident.vehicle), selectinload(FaultIncident.definition))
                    .where(FaultIncident.id == item.incident_id)
                ) if item.category == "fault" else None
                safety_event = await session.scalar(
                    select(SafetyEvent)
                    .options(selectinload(SafetyEvent.vehicle), selectinload(SafetyEvent.driver))
                    .where(SafetyEvent.id == item.safety_event_id)
                ) if item.category == "safety" else None
            if item.category == "fault" and not incident:
                await self._fail(item_id, "Incident no longer exists", dead=True)
                continue
            if item.category == "safety" and not safety_event:
                await self._fail(item_id, "Safety event no longer exists", dead=True)
                continue
            if route is None:
                await self._fail(
                    item_id,
                    "No active Telegram group/thread route for this notification",
                    dead=True,
                )
                continue
            try:
                if item.category == "safety":
                    text = (
                        "📹 <b>Camera media update</b>\n"
                        f"<b>Alert ID:</b> {escape(safety_event.alert_id)}\n"
                        f"<b>Vehicle:</b> "
                        f"{escape(safety_event.vehicle.vehicle_name if safety_event.vehicle and safety_event.vehicle.vehicle_name else 'Unknown')}"
                        if item.notification_type == "media_update"
                        else safety_message(safety_event, self.settings.timezone)
                    )
                    safety_chat_id = route.chat_id
                    safety_topic_id = route.thread_id
                    valid_media = [
                        media for media in safety_event.media_json
                        if isinstance(media, dict) and media.get("url")
                    ]
                    message = None
                    if len(valid_media) == 1:
                        try:
                            media_item = valid_media[0]
                            if str(media_item.get("mediaType", "video")).lower() == "image":
                                message = await self.bot.send_photo(
                                    chat_id=safety_chat_id,
                                    message_thread_id=safety_topic_id,
                                    photo=media_item["url"],
                                    caption=text,
                                    parse_mode="HTML",
                                    reply_markup=None,
                                )
                            else:
                                message = await self.bot.send_video(
                                    chat_id=safety_chat_id,
                                    message_thread_id=safety_topic_id,
                                    video=media_item["url"],
                                    caption=text,
                                    parse_mode="HTML",
                                    reply_markup=None,
                                )
                        except Exception:
                            logger.warning("safety_media_send_failed alert_id=%s", safety_event.alert_id)
                    elif len(valid_media) > 1:
                        try:
                            messages = await self.bot.send_media_group(
                                chat_id=safety_chat_id,
                                message_thread_id=safety_topic_id,
                                media=[
                                    (InputMediaPhoto if str(item.get("mediaType", "video")).lower() == "image" else InputMediaVideo)(
                                        media=item["url"],
                                        caption=text if index == 0 else None,
                                        parse_mode="HTML" if index == 0 else None,
                                    )
                                    for index, item in enumerate(valid_media[:10])
                                ],
                            )
                            message = messages[0]
                        except Exception:
                            logger.warning("safety_media_group_send_failed alert_id=%s", safety_event.alert_id)
                    if message is None:
                        message = await self.bot.send_message(
                            chat_id=safety_chat_id,
                            message_thread_id=safety_topic_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=None,
                        )
                else:
                    text = (
                        cleared_message(incident, self.settings.timezone)
                        if item.notification_type == "cleared"
                        else fault_message(incident, self.settings.timezone)
                    )
                    reply_to = incident.telegram_message_id if item.notification_type == "cleared" else None
                    fault_chat_id = route.chat_id
                    fault_topic_id = route.thread_id
                    message = await self.bot.send_message(
                        chat_id=fault_chat_id,
                        message_thread_id=fault_topic_id,
                        text=text,
                        parse_mode="HTML",
                        reply_to_message_id=reply_to,
                        reply_markup=None,
                    )
            except Exception as exc:
                await self._fail(item_id, f"{type(exc).__name__}: {exc}")
                continue
            async with self.sessions.begin() as session:
                row = await session.get(NotificationOutbox, item_id)
                db_incident = await session.get(FaultIncident, incident.id) if incident else None
                db_safety = await session.get(SafetyEvent, safety_event.id) if safety_event else None
                row.status, row.sent_at, row.last_error, row.processing_at = (
                    "sent",
                    datetime.now(UTC),
                    None,
                    None,
                )
                if db_incident and item.notification_type != "cleared" and not db_incident.telegram_message_id:
                    db_incident.telegram_chat_id = message.chat.id
                    db_incident.telegram_topic_id = fault_topic_id
                    db_incident.telegram_message_id = message.message_id
                if db_safety and not db_safety.telegram_message_id:
                    db_safety.telegram_chat_id = message.chat.id
                    db_safety.telegram_topic_id = safety_topic_id
                    db_safety.telegram_message_id = message.message_id
                session.add(
                    NotificationLog(
                        incident_id=incident.id if incident else None,
                        notification_type=item.notification_type,
                        telegram_chat_id=message.chat.id,
                        telegram_topic_id=(
                            safety_topic_id
                            if safety_event
                            else fault_topic_id
                        ),
                        telegram_message_id=message.message_id,
                        sent_at=datetime.now(UTC),
                        status="sent",
                        created_at=datetime.now(UTC),
                    )
                )
            sent += 1
        return sent

    async def _expand_routes(self, item_id: int) -> None:
        async with self.sessions.begin() as session:
            item = await session.get(NotificationOutbox, item_id)
            payload = dict(item.payload_json or {})
            integration_id = payload.get("integration_id")
            if not integration_id or payload.get("route_id"):
                return
            routes = list(
                (
                    await session.scalars(
                        select(TelegramRoute).where(
                            TelegramRoute.samsara_account_id == integration_id,
                            TelegramRoute.active.is_(True),
                            TelegramRoute.purpose.in_((item.category, "both", "all")),
                        ).order_by(TelegramRoute.id)
                    )
                ).all()
            )
            if not routes:
                item.status = "dead"
                item.last_error = "No active Telegram route for this Samsara account/category"
                item.processing_at = None
                return
            payload["route_id"] = routes[0].id
            item.payload_json = payload
            for route in routes[1:]:
                clone_payload = dict(payload)
                clone_payload["route_id"] = route.id
                session.add(
                    NotificationOutbox(
                        incident_id=item.incident_id,
                        safety_event_id=item.safety_event_id,
                        category=item.category,
                        notification_type=item.notification_type,
                        idempotency_key=f"{item.idempotency_key}:route:{route.id}",
                        payload_json=clone_payload,
                        status="pending",
                        attempt_count=0,
                        next_attempt_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                )

    async def _fail(self, item_id: int, error: str, dead: bool = False) -> None:
        async with self.sessions.begin() as session:
            item = await session.get(NotificationOutbox, item_id)
            item.attempt_count += 1
            item.last_error = error[:2000]
            item.status = "dead" if dead or item.attempt_count >= 10 else "failed"
            item.processing_at = None
            item.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(2 ** item.attempt_count, 300))
        logger.error("telegram_notification_failed outbox_id=%d error_type=%s", item_id, error.split(":", 1)[0])

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.send_pending_once()
            try:
                await asyncio.wait_for(self._stop.wait(), 2)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
