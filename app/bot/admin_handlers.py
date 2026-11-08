from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import SamsaraAccount, TelegramRoute
from app.samsara.client import SamsaraClient
from app.security import TokenCipher


class AddSamsara(StatesGroup):
    name = State()
    token = State()


def build_admin_router(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> Router:
    router = Router()
    cipher = TokenCipher(settings.token_encryption_key)

    def admin(message: Message) -> bool:
        user = message.from_user
        return bool(user) and settings.is_admin(user.id, user.username)

    async def account_named(session: AsyncSession, name: str) -> SamsaraAccount | None:
        return await session.scalar(
            select(SamsaraAccount).where(func.lower(SamsaraAccount.name) == name.lower())
        )

    @router.message(Command("help", ignore_case=True))
    async def admin_help(message: Message) -> None:
        if not admin(message):
            await message.answer("Not authorized.")
            return
        await message.answer(
            "<b>Samsara bot admin commands</b>\n\n"
            "<b>Private chat with the bot</b>\n"
            "/addSamsara — add and validate a Samsara API account\n"
            "/removeSamsara &lt;name&gt; — disable an account and its routes\n"
            "/samsaras — list configured Samsara accounts\n\n"
            "<b>Group or exact forum thread</b>\n"
            "/activate — route fault codes, safety alerts, fuel notifications, or all\n"
            "/deactivate — disable routes only in this exact group/thread\n"
            "/setup_topics — create Faults, Safety and Fuel topics in this group\n\n"
            "<b>Information and testing</b>\n"
            "/help — show this admin command list\n"
            "/fault_help — show fault lookup commands\n"
            "/safety — show recent safety alerts\n"
            "/currentfuel — show current fuel, sorted low to high\n"
            "/safety_test — test safety-message delivery\n"
            "/fault_test — test fault-message delivery",
            parse_mode="HTML",
        )

    @router.message(Command("addSamsara", ignore_case=True))
    async def add_start(message: Message, state: FSMContext) -> None:
        if message.chat.type != "private" or not admin(message):
            await message.answer("Admin-only command. Use it in the bot's private chat.")
            return
        await state.set_state(AddSamsara.name)
        await message.answer("Send a custom name for this Samsara account.")

    @router.message(AddSamsara.name)
    async def add_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name or len(name) > 128:
            await message.answer("Name must be between 1 and 128 characters.")
            return
        async with sessions() as session:
            existing = await account_named(session, name)
            if existing and existing.active:
                await message.answer("That Samsara name already exists.")
                return
        await state.update_data(name=name, existing_id=existing.id if existing else None)
        await state.set_state(AddSamsara.token)
        await message.answer("Send the Samsara API token. I will validate and encrypt it.")

    @router.message(AddSamsara.token)
    async def add_token(message: Message, state: FSMContext) -> None:
        token = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        client = SamsaraClient(
            token,
            settings.samsara_base_url,
            settings.samsara_request_timeout_seconds,
            settings.samsara_max_retries,
        )
        try:
            await client.get_fault_code_feed()
        except Exception as exc:
            await message.answer(f"Token validation failed: {type(exc).__name__}. Try again.")
            return
        finally:
            await client.close()
        data = await state.get_data()
        now = datetime.now(UTC)
        async with sessions.begin() as session:
            existing = await session.get(SamsaraAccount, data.get("existing_id")) if data.get("existing_id") else None
            if existing:
                existing.encrypted_api_token = cipher.encrypt(token)
                existing.active = True
                existing.updated_at = now
            else:
                session.add(SamsaraAccount(
                    name=data["name"],
                    encrypted_api_token=cipher.encrypt(token),
                    active=True,
                    created_by_telegram_id=message.from_user.id if message.from_user else None,
                    created_at=now,
                    updated_at=now,
                ))
        await state.clear()
        await message.answer(f"Samsara account '{data['name']}' added.")

    @router.message(Command("samsaras", ignore_case=True))
    async def list_accounts(message: Message) -> None:
        if not admin(message):
            await message.answer("Not authorized.")
            return
        async with sessions() as session:
            rows = list((await session.scalars(select(SamsaraAccount).order_by(SamsaraAccount.name))).all())
        text = "\n".join(f"• {row.name} ({'active' if row.active else 'disabled'})" for row in rows)
        await message.answer(text or "No Samsara accounts configured.")

    @router.message(Command("removeSamsara", ignore_case=True))
    async def remove_account(message: Message) -> None:
        if message.chat.type != "private" or not admin(message):
            await message.answer("Admin-only command. Use it in private chat.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Usage: /removeSamsara account name")
            return
        async with sessions.begin() as session:
            row = await account_named(session, parts[1].strip())
            if not row:
                await message.answer("Samsara account not found.")
                return
            row.active = False
            row.updated_at = datetime.now(UTC)
            routes = list((await session.scalars(select(TelegramRoute).where(TelegramRoute.samsara_account_id == row.id))).all())
            for route in routes:
                route.active = False
        await message.answer(f"Samsara account '{row.name}' and its routes were disabled.")

    # /activate uses inline buttons. Button taps (callback queries) reach the
    # bot even when its group privacy mode is on; typed replies do not.
    purpose_labels = {
        "fault": "Fault codes",
        "safety": "Safety alerts",
        "fuel": "Fuel notifications",
        "both": "Both",
        "all": "All",
    }

    def callback_admin(callback: CallbackQuery) -> bool:
        user = callback.from_user
        return bool(user) and settings.is_admin(user.id, user.username)

    def route_thread_id(message: Message) -> int | None:
        return message.message_thread_id if message.is_topic_message else None

    @router.message(Command("activate", ignore_case=True))
    async def activate_start(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"} or not admin(message):
            await message.answer("Admin-only command. Use it in the destination group/thread.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=purpose_labels["fault"], callback_data="activate:p:fault"),
                    InlineKeyboardButton(text=purpose_labels["safety"], callback_data="activate:p:safety"),
                ],
                [
                    InlineKeyboardButton(text=purpose_labels["fuel"], callback_data="activate:p:fuel"),
                    InlineKeyboardButton(text=purpose_labels["both"], callback_data="activate:p:both"),
                ],
                [InlineKeyboardButton(text=purpose_labels["all"], callback_data="activate:p:all")],
                [InlineKeyboardButton(text="Cancel", callback_data="activate:cancel")],
            ]
        )
        await message.answer("Choose what this exact group/thread receives:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("activate:"))
    async def activate_callback(callback: CallbackQuery) -> None:
        message = callback.message
        if not isinstance(message, Message) or message.chat.type not in {"group", "supergroup"}:
            await callback.answer()
            return
        if not callback_admin(callback):
            await callback.answer("Admin-only.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if parts[1:2] == ["cancel"]:
            await message.edit_text("Activation cancelled.")
            await callback.answer()
            return
        if len(parts) == 3 and parts[1] == "p" and parts[2] in purpose_labels:
            purpose = parts[2]
            async with sessions() as session:
                accounts = list(
                    (
                        await session.scalars(
                            select(SamsaraAccount)
                            .where(SamsaraAccount.active.is_(True))
                            .order_by(SamsaraAccount.name)
                        )
                    ).all()
                )
            if not accounts:
                await message.edit_text("No active Samsara accounts. Add one with /addSamsara in private chat.")
                await callback.answer()
                return
            rows = [
                [InlineKeyboardButton(text=account.name, callback_data=f"activate:a:{purpose}:{account.id}")]
                for account in accounts[:50]
            ]
            rows.append([InlineKeyboardButton(text="Cancel", callback_data="activate:cancel")])
            await message.edit_text(
                f"{purpose_labels[purpose]} — choose the Samsara account:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            await callback.answer()
            return
        if len(parts) == 4 and parts[1] == "a" and parts[2] in purpose_labels and parts[3].isdigit():
            purpose, account_id = parts[2], int(parts[3])
            chat_id, thread_id = message.chat.id, route_thread_id(message)
            async with sessions.begin() as session:
                account = await session.get(SamsaraAccount, account_id)
                if not account or not account.active:
                    await callback.answer("That Samsara account is no longer active.", show_alert=True)
                    return
                route = await session.scalar(
                    select(TelegramRoute).where(
                        TelegramRoute.samsara_account_id == account.id,
                        TelegramRoute.chat_id == chat_id,
                        TelegramRoute.thread_id == thread_id
                        if thread_id is not None
                        else TelegramRoute.thread_id.is_(None),
                    )
                )
                now = datetime.now(UTC)
                if route:
                    route.purpose, route.active, route.updated_at = purpose, True, now
                else:
                    session.add(
                        TelegramRoute(
                            samsara_account_id=account.id,
                            chat_id=chat_id,
                            thread_id=thread_id,
                            chat_title=message.chat.title,
                            purpose=purpose,
                            active=True,
                            created_by_telegram_id=callback.from_user.id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                account_name = account.name
            await message.edit_text(
                f"Activated {purpose_labels[purpose]} for '{account_name}' in this exact thread."
            )
            await callback.answer("Activated.")
            return
        await callback.answer()

    # /setup_topics: one forum topic per alert category in this group.
    topic_plan = [
        ("fault", "🛠 Faults", 0xFB6F5F),
        ("safety", "⚠️ Safety", 0xFFD67E),
        ("fuel", "⛽ Fuel", 0x6FB9F0),
    ]
    topic_intro = {
        "fault": "This topic receives fault code alerts for '{name}'.",
        "safety": "This topic receives safety alerts for '{name}'.",
        "fuel": "Fuel topic for '{name}'. Use /currentfuel here.",
    }

    @router.message(Command("setup_topics", ignore_case=True))
    async def setup_topics_start(message: Message) -> None:
        if message.chat.type != "supergroup" or not admin(message):
            await message.answer("Admin-only command. Use it in the fleet group.")
            return
        if not message.chat.is_forum:
            await message.answer(
                "Topics are off in this group. Turn on Topics in the group settings, then run /setup_topics again."
            )
            return
        async with sessions() as session:
            accounts = list(
                (
                    await session.scalars(
                        select(SamsaraAccount)
                        .where(SamsaraAccount.active.is_(True))
                        .order_by(SamsaraAccount.name)
                    )
                ).all()
            )
        if not accounts:
            await message.answer("No active Samsara accounts. Add one with /addSamsara in private chat.")
            return
        rows = [
            [InlineKeyboardButton(text=account.name, callback_data=f"topics:{account.id}")]
            for account in accounts[:50]
        ]
        rows.append([InlineKeyboardButton(text="Cancel", callback_data="topics:cancel")])
        await message.answer(
            "Create Faults, Safety and Fuel topics in this group for which Samsara account?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("topics:"))
    async def setup_topics_callback(callback: CallbackQuery) -> None:
        message = callback.message
        if not isinstance(message, Message) or message.chat.type != "supergroup":
            await callback.answer()
            return
        if not callback_admin(callback):
            await callback.answer("Admin-only.", show_alert=True)
            return
        value = (callback.data or "").split(":", 1)[1]
        if value == "cancel":
            await message.edit_text("Topic setup cancelled.")
            await callback.answer()
            return
        if not value.isdigit():
            await callback.answer()
            return
        chat_id = message.chat.id
        async with sessions() as session:
            account = await session.get(SamsaraAccount, int(value))
            if not account or not account.active:
                await callback.answer("That Samsara account is no longer active.", show_alert=True)
                return
            account_id, account_name = account.id, account.name
            existing = list(
                (
                    await session.scalars(
                        select(TelegramRoute).where(
                            TelegramRoute.samsara_account_id == account_id,
                            TelegramRoute.chat_id == chat_id,
                            TelegramRoute.active.is_(True),
                        )
                    )
                ).all()
            )
        # Re-running reuses topics this command already created.
        have = {
            route.purpose: route.thread_id
            for route in existing
            if route.purpose in {"fault", "safety", "fuel"} and route.thread_id is not None
        }
        await callback.answer("Setting up topics…")
        created: dict[str, int] = {}
        for purpose, title, color in topic_plan:
            if purpose in have:
                continue
            try:
                topic = await message.bot.create_forum_topic(
                    chat_id=chat_id, name=title, icon_color=color
                )
            except Exception as exc:
                await message.edit_text(
                    f"Couldn't create the {title} topic ({type(exc).__name__}). "
                    "Make sure the bot is an admin with the 'Manage topics' right, then run /setup_topics again."
                )
                if not created:
                    return
                break
            created[purpose] = topic.message_thread_id
        threads = {**have, **created}
        now = datetime.now(UTC)
        disabled = 0
        async with sessions.begin() as session:
            routes = list(
                (
                    await session.scalars(
                        select(TelegramRoute).where(
                            TelegramRoute.samsara_account_id == account_id,
                            TelegramRoute.chat_id == chat_id,
                        )
                    )
                ).all()
            )
            by_thread = {route.thread_id: route for route in routes}
            for purpose, thread_id in threads.items():
                route = by_thread.get(thread_id)
                if route:
                    route.purpose, route.active, route.updated_at = purpose, True, now
                else:
                    session.add(
                        TelegramRoute(
                            samsara_account_id=account_id,
                            chat_id=chat_id,
                            thread_id=thread_id,
                            chat_title=message.chat.title,
                            purpose=purpose,
                            active=True,
                            created_by_telegram_id=callback.from_user.id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            # Older routes in this group (e.g. "All" in General) would now
            # duplicate every alert, so turn them off.
            for route in routes:
                if route.active and route.thread_id not in threads.values():
                    route.active, route.updated_at = False, now
                    disabled += 1
        for purpose, thread_id in created.items():
            try:
                await message.bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=topic_intro[purpose].format(name=account_name),
                )
            except Exception:
                pass
        names = {purpose: title for purpose, title, _ in topic_plan}
        lines = [f"Topics for '{account_name}':"]
        for purpose, _, _ in topic_plan:
            if purpose in threads:
                state = "created" if purpose in created else "already set up"
                lines.append(f"• {names[purpose]} — {state}")
            else:
                lines.append(f"• {names[purpose]} — not created")
        if disabled:
            lines.append(f"Turned off {disabled} older route(s) in this group so alerts aren't sent twice.")
        await message.edit_text("\n".join(lines))

    @router.message(Command("deactivate", ignore_case=True))
    async def deactivate(message: Message) -> None:
        if message.chat.type not in {"group", "supergroup"} or not admin(message):
            await message.answer("Admin-only command.")
            return
        async with sessions.begin() as session:
            routes = list((await session.scalars(select(TelegramRoute).where(TelegramRoute.chat_id == message.chat.id, TelegramRoute.thread_id == message.message_thread_id, TelegramRoute.active.is_(True)))).all())
            for route in routes:
                route.active, route.updated_at = False, datetime.now(UTC)
        await message.answer(f"Deactivated {len(routes)} route(s) in this exact thread.")

    return router
