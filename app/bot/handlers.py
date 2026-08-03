from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.formatters import fault_message, truck_name
from app.bot.keyboards import incident_keyboard
from app.config import Settings
from app.database.models import FaultEvent, FaultIncident, Vehicle


def build_router(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> Router:
    router = Router()

    def authorized(user: object | None) -> bool:
        return bool(user) and settings.is_authorized(
            getattr(user, "id", None), getattr(user, "username", None)
        )

    async def load_issue(issue_id: str) -> FaultIncident | None:
        async with sessions() as session:
            return await session.scalar(
                select(FaultIncident)
                .options(selectinload(FaultIncident.vehicle), selectinload(FaultIncident.definition))
                .where(FaultIncident.issue_id == issue_id.upper())
            )

    @router.message(Command("fault_help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "/active_faults — active incidents\n/fault FLT-YYYYMMDD-NNNN — incident details\n"
            "/faults [truck] — active faults\n/fault_history <truck> — recent history\n"
            "/fault_summary — fleet summary\n/fault_stats — statistics\n/fault_test — test alert"
        )

    @router.message(Command("active_faults", "faults", "critical_faults"))
    async def active_faults(message: Message, command: CommandObject) -> None:
        truck = command.args.strip() if command.args else None
        async with sessions() as session:
            stmt = (
                select(FaultIncident)
                .join(FaultIncident.vehicle)
                .options(selectinload(FaultIncident.vehicle), selectinload(FaultIncident.definition))
                .where(FaultIncident.diagnostic_status == "active")
                .order_by(FaultIncident.severity, FaultIncident.first_seen_at)
                .limit(30)
            )
            if message.text and message.text.startswith("/critical_faults"):
                stmt = stmt.where(FaultIncident.severity == "critical")
            if truck:
                stmt = stmt.where(
                    (Vehicle.unit_number == truck) | (Vehicle.vehicle_name == truck) | (Vehicle.vin == truck)
                )
            incidents = list((await session.scalars(stmt)).all())
        if not incidents:
            await message.answer("No matching active faults.")
            return
        lines = [f"🚨 <b>Active faults: {len(incidents)}</b>", ""]
        for index, incident in enumerate(incidents, 1):
            lines.append(
                f"{index}. <b>{escape(incident.issue_id)}</b> — {escape(truck_name(incident))}\n"
                f"{escape(incident.definition.normalized_code)} · {escape(incident.severity.title())}"
            )
        await message.answer("\n\n".join(lines), parse_mode="HTML")

    @router.message(Command("fault"))
    async def fault_detail(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Usage: /fault FLT-YYYYMMDD-NNNN")
            return
        incident = await load_issue(command.args.strip())
        if not incident:
            await message.answer("Issue not found.")
            return
        await message.answer(
            fault_message(incident, settings.timezone),
            parse_mode="HTML",
            reply_markup=incident_keyboard(incident.issue_id),
        )

    @router.message(Command("fault_summary", "fault_stats"))
    async def summary(message: Message) -> None:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(FaultIncident.severity, func.count())
                    .where(FaultIncident.diagnostic_status == "active")
                    .group_by(FaultIncident.severity)
                )
            ).all()
            active = sum(count for _, count in rows)
            affected = await session.scalar(
                select(func.count(func.distinct(FaultIncident.vehicle_id))).where(
                    FaultIncident.diagnostic_status == "active"
                )
            )
        details = "\n".join(f"{severity.title()}: {count}" for severity, count in rows)
        await message.answer(
            f"<b>Fault summary</b>\nActive incidents: {active}\nAffected trucks: {affected or 0}\n{escape(details)}",
            parse_mode="HTML",
        )

    @router.message(Command("fault_test"))
    async def fault_test(message: Message) -> None:
        if not authorized(message.from_user):
            await message.answer("Not authorized.")
            return
        await message.answer(
            "<b>🧪 TEST FAULT — NOT A REAL VEHICLE ALERT</b>\n\n"
            "Telegram delivery and topic routing are working.",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("fault:"))
    async def action(callback: CallbackQuery) -> None:
        if not authorized(callback.from_user):
            await callback.answer("Not authorized.", show_alert=True)
            return
        parts = (callback.data or "").split(":", 2)
        if len(parts) != 3:
            await callback.answer("Invalid action.", show_alert=True)
            return
        _, action_name, issue_id = parts
        if action_name in {"note", "assign"}:
            await callback.answer(
                f"Use /fault {issue_id} and record {'a note' if action_name == 'note' else 'assignment'} through maintenance workflow.",
                show_alert=True,
            )
            return
        if action_name == "ignore":
            await callback.message.edit_reply_markup(reply_markup=incident_keyboard(issue_id, True))
            await callback.answer()
            return
        transitions = {"ack": "acknowledged", "start": "in_progress", "ignore_yes": "ignored"}
        async with sessions.begin() as session:
            incident = await session.scalar(
                select(FaultIncident).where(FaultIncident.issue_id == issue_id)
            )
            if not incident:
                await callback.answer("Issue not found.", show_alert=True)
                return
            if action_name in transitions:
                incident.maintenance_status = transitions[action_name]
                incident.updated_at = datetime.now(UTC)
                if action_name == "ack":
                    incident.acknowledged_at = datetime.now(UTC)
                    incident.acknowledged_by_telegram_id = callback.from_user.id
                    incident.acknowledged_by_name = callback.from_user.full_name
                session.add(
                    FaultEvent(
                        incident_id=incident.id,
                        event_type=transitions[action_name],
                        received_at=datetime.now(UTC),
                        raw_event_json={"telegram_user_id": callback.from_user.id},
                        created_at=datetime.now(UTC),
                    )
                )
        await callback.answer("Updated.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=incident_keyboard(issue_id))

    return router
