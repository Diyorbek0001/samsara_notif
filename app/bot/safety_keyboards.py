from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def safety_keyboard(alert_id: str, map_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Acknowledge", callback_data=f"safety:ack:{alert_id}"),
            InlineKeyboardButton(text="👁 Reviewed", callback_data=f"safety:review:{alert_id}"),
        ],
        [
            InlineKeyboardButton(text="🎓 Coached", callback_data=f"safety:coach:{alert_id}"),
            InlineKeyboardButton(text="🚫 Dismiss", callback_data=f"safety:dismiss:{alert_id}"),
        ],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"safety:refresh:{alert_id}")],
    ]
    if map_url:
        rows[-1].append(InlineKeyboardButton(text="🔗 Open Map", url=map_url))
    return InlineKeyboardMarkup(inline_keyboard=rows)

