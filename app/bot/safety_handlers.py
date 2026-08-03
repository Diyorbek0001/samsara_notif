from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.safety_formatters import safety_message
from app.bot.routing import SAFETY_PRIVATE_CHAT_KEY
from app.config import Settings
from app.database.models import NotificationOutbox, SafetyEvent, SafetyEventUpdate
from app.faults.service import state_get, state_set


def build_safety_router(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> Router:
    router = Router()

    def authorized(user: object | None) -> bool:
        return bool(user) and settings.is_authorized(
            getattr(user, "id", None), getattr(user, "username", None)
        )

    async def load(alert_id: str) -> SafetyEvent | None:
        async with sessions() as session:
            return await session.scalar(
                select(SafetyEvent)
                .options(selectinload(SafetyEvent.vehicle), selectinload(SafetyEvent.driver))
                .where(SafetyEvent.alert_id == alert_id.upper())
            )

    @router.message(Command("start"))
    async def start_private_alerts(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Please send /start to me in a private chat.")
            return
        queued = 0
        async with sessions.begin() as session:
            existing = await state_get(session, SAFETY_PRIVATE_CHAT_KEY)
            if existing and existing != str(message.chat.id) and not authorized(message.from_user):
                await message.answer("A private safety-alert chat is already registered.")
                return
            if not existing and (settings.authorized_ids or settings.authorized_usernames) and not authorized(message.from_user):
                await message.answer("Not authorized.")
                return
            await state_set(session, SAFETY_PRIVATE_CHAT_KEY, str(message.chat.id))
            recent = list(
                (
                    await session.scalars(
                        select(SafetyEvent)
                        .order_by(SafetyEvent.event_timestamp.desc())
                        .limit(20)
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for event in recent:
                idempotency_key = (
                    f"safety:private:{message.chat.id}:{event.samsara_event_id}"
                )
                already_queued = await session.scalar(
                    select(NotificationOutbox.id).where(
                        NotificationOutbox.idempotency_key == idempotency_key
                    )
                )
                if already_queued:
                    continue
                session.add(
                    NotificationOutbox(
                        safety_event_id=event.id,
                        category="safety",
                        notification_type="private_replay",
                        idempotency_key=idempotency_key,
                        payload_json={
                            "safety_event_id": event.id,
                            "alert_id": event.alert_id,
                        },
                        status="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
                queued += 1
        await message.answer(
            "Safety alerts and dashcam videos will be sent to this private chat. "
            "Truck-fault notifications will continue going to the fleet group. "
            f"Queued {queued} recent safety alert(s) for testing."
        )

    @router.message(Command("safety_today", "safety_active"))
    async def safety_list(message: Message) -> None:
        async with sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SafetyEvent)
                        .options(selectinload(SafetyEvent.vehicle), selectinload(SafetyEvent.driver))
                        .order_by(SafetyEvent.event_timestamp.desc())
                        .limit(20)
                    )
                ).all()
            )
        if not rows:
            await message.answer("No safety events stored.")
            return
        text = ["🛡 <b>Recent safety alerts</b>", ""]
        for item in rows:
            text.append(
                f"<b>{escape(item.alert_id)}</b> — {escape(item.primary_behavior)} "
                f"({escape(item.severity.title())})"
            )
        await message.answer("\n".join(text), parse_mode="HTML")

    @router.message(Command("safety_summary", "safety_stats"))
    async def safety_summary(message: Message) -> None:
        async with sessions() as session:
            total = await session.scalar(select(func.count()).select_from(SafetyEvent))
            rows = (
                await session.execute(
                    select(SafetyEvent.severity, func.count()).group_by(SafetyEvent.severity)
                )
            ).all()
        details = "\n".join(f"{level.title()}: {count}" for level, count in rows)
        await message.answer(
            f"🛡 <b>Safety summary</b>\nTotal alerts: {total or 0}\n{escape(details)}",
            parse_mode="HTML",
        )

    @router.message(Command("safety_test"))
    async def safety_test(message: Message) -> None:
        if not authorized(message.from_user):
            await message.answer("Not authorized.")
            return
        await message.answer(
            "<b>🧪 TEST — NOT A REAL FLEET ALERT</b>\n\n"
            "Safety-event Telegram routing is working.",
            parse_mode="HTML",
        )

    @router.message(Command("safety"))
    async def safety_detail(message: Message, command: CommandObject) -> None:
        if not command.args:
            await safety_list(message)
            return
        item = await load(command.args.strip())
        if not item:
            await message.answer("Safety alert not found.")
            return
        await message.answer(safety_message(item, settings.timezone), parse_mode="HTML")

    @router.callback_query(F.data.startswith("safety:"))
    async def safety_action(callback: CallbackQuery) -> None:
        if not authorized(callback.from_user):
            await callback.answer("Not authorized.", show_alert=True)
            return
        parts = (callback.data or "").split(":", 2)
        if len(parts) != 3:
            await callback.answer("Invalid action.", show_alert=True)
            return
        _, action, alert_id = parts
        transitions = {
            "ack": ("acknowledged", None),
            "review": ("reviewed", None),
            "coach": ("reviewed", "completed"),
            "dismiss": ("dismissed", "not_required"),
        }
        if action == "refresh":
            await callback.answer("Media refresh queued for the next event update.")
            return
        if action not in transitions:
            await callback.answer("Invalid action.", show_alert=True)
            return
        async with sessions.begin() as session:
            item = await session.scalar(
                select(SafetyEvent).where(SafetyEvent.alert_id == alert_id)
            )
            if not item:
                await callback.answer("Alert not found.", show_alert=True)
                return
            review, coaching = transitions[action]
            item.review_status = review
            if coaching:
                item.coaching_status = coaching
            if action == "ack":
                item.acknowledged_at = datetime.now(UTC)
                item.acknowledged_by_telegram_id = callback.from_user.id
                item.acknowledged_by_name = callback.from_user.full_name
            item.updated_at = datetime.now(UTC)
            session.add(
                SafetyEventUpdate(
                    safety_event_id=item.id,
                    event_type=review,
                    received_at=datetime.now(UTC),
                    raw_update_json={"telegram_user_id": callback.from_user.id},
                    created_at=datetime.now(UTC),
                )
            )
        await callback.answer("Updated.")

    return router
