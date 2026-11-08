from datetime import UTC, datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, CreateForumTopic, EditForumTopic, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, ForumTopic, Message, User
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.bot.admin_handlers import build_admin_router
from app.config import Settings
from app.database.models import SamsaraAccount, TelegramRoute
from app.database.session import create_database, create_tables
from app.safety.topics import SAFETY_TOPICS

CHAT = Chat(id=-1001, type="supergroup", title="Fleet A", is_forum=True)


class FakeSession(BaseSession):
    """Answers Bot API calls locally and records them."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_thread = 500

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, CreateForumTopic):
            self.next_thread += 1
            return ForumTopic(message_thread_id=self.next_thread, name=method.name, icon_color=method.icon_color or 0)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=900, date=datetime.now(UTC), chat=CHAT, text="ok")
        if isinstance(method, (AnswerCallbackQuery, EditForumTopic)):
            return True
        raise AssertionError(f"unexpected call {type(method).__name__}")

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError


def _handler(router, name):
    for observer in (router.message, router.callback_query):
        for handler in observer.handlers:
            if handler.callback.__name__ == name:
                return handler.callback
    raise LookupError(name)


async def test_setup_topics_creates_three_routed_topics(tmp_path) -> None:
    settings = Settings(token_encryption_key=Fernet.generate_key().decode(), admin_telegram_ids="111")
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await create_tables(engine)
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        account = SamsaraAccount(name="acb", encrypted_api_token="x", active=True, created_at=now, updated_at=now)
        session.add(account)
        await session.flush()
        # An older "All" route in General that would duplicate alerts.
        session.add(TelegramRoute(samsara_account_id=account.id, chat_id=CHAT.id, thread_id=None,
                                  purpose="all", active=True, created_at=now, updated_at=now))
        account_id = account.id

    fake = FakeSession()
    bot = Bot("123:abc", session=fake)
    router = build_admin_router(settings, sessions)
    admin = User(id=111, is_bot=False, first_name="Admin")
    bot_message = Message(message_id=10, date=now, chat=CHAT, text="pick").as_(bot)
    callback = CallbackQuery(id="1", from_user=admin, chat_instance="c", message=bot_message,
                             data=f"topics:{account_id}").as_(bot)

    await _handler(router, "setup_topics_callback")(callback)

    created = [c.name for c in fake.calls if isinstance(c, CreateForumTopic)]
    assert created == ["🛠 Fault Codes"] + [topic.title for topic in SAFETY_TOPICS]
    assert "👀 Distractions" in created
    assert not {"📵 Phone Usage", "😴 Drowsiness"} & set(created)
    assert "🏎 Speeding" in created and "📷 Camera Obstruction" in created
    async with sessions() as session:
        routes = list((await session.scalars(select(TelegramRoute))).all())
    active = {r.purpose: r.thread_id for r in routes if r.active}
    assert active["fault"] == 501
    assert active["s:speeding"] == 502
    assert set(active) == {"fault"} | {topic.purpose for topic in SAFETY_TOPICS}
    assert [r.purpose for r in routes if not r.active] == ["all"]
    summary = [c for c in fake.calls if isinstance(c, EditMessageText)][-1].text
    assert "Turned off 1 older route" in summary

    # Running it again reuses the topics instead of creating new ones.
    fake.calls.clear()
    await _handler(router, "setup_topics_callback")(callback)
    assert not [c for c in fake.calls if isinstance(c, CreateForumTopic)]
    renamed = {c.message_thread_id: c.name for c in fake.calls if isinstance(c, EditForumTopic)}
    assert renamed[501] == "🛠 Fault Codes"
    assert "already set up" in [c for c in fake.calls if isinstance(c, EditMessageText)][-1].text
