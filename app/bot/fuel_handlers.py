from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.database.models import SamsaraAccount, TelegramRoute
from app.samsara.client import SamsaraClient
from app.security import TokenCipher


@dataclass(frozen=True)
class FuelSnapshotRow:
    unit_name: str
    latitude: float | None
    longitude: float | None
    fuel_percent: float | None


def _latest_stat(stats: Any) -> dict[str, Any] | None:
    if isinstance(stats, list):
        candidates = [item for item in stats if isinstance(item, dict)]
        if not candidates:
            return None
        return candidates[-1]
    if isinstance(stats, dict):
        return stats
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unit_name(vehicle: dict[str, Any]) -> str:
    for key in ("name", "vehicleName", "unitNumber", "id"):
        value = vehicle.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Unknown unit"


def parse_fuel_snapshot(payload: list[dict[str, Any]]) -> list[FuelSnapshotRow]:
    rows: list[FuelSnapshotRow] = []
    for vehicle in payload:
        gps = _latest_stat(vehicle.get("gps"))
        fuel = _latest_stat(vehicle.get("fuelPercents"))
        fuel_value = None
        if fuel:
            fuel_value = _number(fuel.get("value") if "value" in fuel else fuel.get("fuelPercent"))
        rows.append(
            FuelSnapshotRow(
                unit_name=_unit_name(vehicle),
                latitude=_number(gps.get("latitude")) if gps else None,
                longitude=_number(gps.get("longitude")) if gps else None,
                fuel_percent=fuel_value,
            )
        )
    return sorted(
        rows,
        key=lambda row: (
            row.fuel_percent is None,
            row.fuel_percent if row.fuel_percent is not None else 101,
            row.unit_name.lower(),
        ),
    )


def fuel_snapshot_message(rows: list[FuelSnapshotRow]) -> str:
    if not rows:
        return "No current fuel data was returned by Samsara."
    lines = ["⛽ <b>Current fuel levels</b>", ""]
    for index, row in enumerate(rows, 1):
        fuel = "unknown" if row.fuel_percent is None else f"{row.fuel_percent:g}%"
        location = (
            "unknown coordinates"
            if row.latitude is None or row.longitude is None
            else f"{row.latitude:.6f}, {row.longitude:.6f}"
        )
        lines.append(
            f"{index}. <b>{escape(row.unit_name)}</b> — {escape(fuel)}\n"
            f"   {escape(location)}"
        )
    return "\n".join(lines)


def build_fuel_router(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> Router:
    router = Router()
    cipher = TokenCipher(settings.token_encryption_key)

    def authorized(user: object | None) -> bool:
        return bool(user) and settings.is_authorized(
            getattr(user, "id", None), getattr(user, "username", None)
        )

    @router.message(Command("currentfuel", ignore_case=True))
    async def current_fuel(message: Message) -> None:
        if not authorized(message.from_user):
            await message.answer("Not authorized.")
            return
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("Use /currentfuel in an activated group or topic.")
            return
        async with sessions() as session:
            route = await session.scalar(
                select(TelegramRoute)
                .join(TelegramRoute.samsara_account)
                .options(selectinload(TelegramRoute.samsara_account))
                .where(
                    TelegramRoute.chat_id == message.chat.id,
                    TelegramRoute.thread_id == message.message_thread_id,
                    TelegramRoute.active.is_(True),
                    TelegramRoute.purpose.in_(("fuel", "all")),
                    SamsaraAccount.active.is_(True),
                )
                .order_by(TelegramRoute.id)
            )
        if not route:
            await message.answer("Fuel notifications are not activated in this group/thread.")
            return
        client = SamsaraClient(
            cipher.decrypt(route.samsara_account.encrypted_api_token),
            settings.samsara_base_url,
            settings.samsara_request_timeout_seconds,
            settings.samsara_max_retries,
        )
        try:
            snapshot = await client.get_current_fuel_snapshot()
        finally:
            await client.close()
        await message.answer(
            fuel_snapshot_message(parse_fuel_snapshot(snapshot)),
            parse_mode="HTML",
        )

    return router
