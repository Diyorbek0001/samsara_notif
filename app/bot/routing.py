SAFETY_PRIVATE_CHAT_KEY = "telegram_safety_private_chat_id"


async def migrate_chat_routes(session, old_chat_id: int, new_chat_id: int) -> tuple[int, int]:
    """Move routes from a group Telegram converted to a supergroup.

    Turning on Topics (and some other settings) converts a basic group into a
    supergroup with a new chat ID; the old ID then rejects every message.
    Routes are moved to the new ID. If the new group already has active routes
    for the same Samsara account (e.g. from /setup_topics), the old route is
    turned off instead so alerts are not duplicated into General.

    Returns (moved, disabled).
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.database.models import TelegramRoute

    if old_chat_id == new_chat_id:
        return 0, 0
    old_routes = list(
        (await session.scalars(select(TelegramRoute).where(TelegramRoute.chat_id == old_chat_id))).all()
    )
    new_routes = list(
        (await session.scalars(select(TelegramRoute).where(TelegramRoute.chat_id == new_chat_id))).all()
    )
    taken = {(r.samsara_account_id, r.thread_id) for r in new_routes}
    accounts_with_routes = {r.samsara_account_id for r in new_routes if r.active}
    now = datetime.now(UTC)
    moved = disabled = 0
    for route in old_routes:
        if (
            route.samsara_account_id in accounts_with_routes
            or (route.samsara_account_id, route.thread_id) in taken
        ):
            if route.active:
                route.active, route.updated_at = False, now
                disabled += 1
            continue
        route.chat_id, route.updated_at = new_chat_id, now
        taken.add((route.samsara_account_id, route.thread_id))
        moved += 1
    return moved, disabled
