from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def incident_keyboard(issue_id: str, ignored_confirmation: bool = False) -> InlineKeyboardMarkup:
    if ignored_confirmation:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Confirm ignore", callback_data=f"fault:ignore_yes:{issue_id}"),
                    InlineKeyboardButton(text="Cancel", callback_data=f"fault:refresh:{issue_id}"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Acknowledge", callback_data=f"fault:ack:{issue_id}"),
                InlineKeyboardButton(text="🔧 Start Work", callback_data=f"fault:start:{issue_id}"),
            ],
            [
                InlineKeyboardButton(text="📝 Add Note", callback_data=f"fault:note:{issue_id}"),
                InlineKeyboardButton(text="👤 Assign", callback_data=f"fault:assign:{issue_id}"),
            ],
            [
                InlineKeyboardButton(text="🚫 Ignore", callback_data=f"fault:ignore:{issue_id}"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data=f"fault:refresh:{issue_id}"),
            ],
        ]
    )

