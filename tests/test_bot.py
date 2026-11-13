"""Telegram-бот: настройка компании без кода, поручения AI (ТЕСТОВАЯ модель), согласование, доработка,
комментарии из группы обсуждения. Bot API замокан."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.ai.connectors.base import MockConnector
from app.ai.connectors.dev_mock import DevMockConnector
from app.ai.orchestrator import Orchestrator
from app.bot.bot import GlassBot
from app.core.config import get_settings
from app.db.models import Company, CompanyMember, InboxItem, Post, User
from app.rag.index import HybridSearcher
from app.services import posts as P
from app.services.publisher import LogNotifier, TelegramBotAPI
from app.services.sources import create_company

from .conftest import HashEmbedder


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, req):
        body = json.loads(req.content or b"{}") if req.headers.get("content-type", "").startswith("application/json") \
            else {}
        self.calls.append((req.url.path.rsplit("/", 1)[-1], body))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 555}})

    def texts(self):
        return [d.get("text", "") for m, d in self.calls if m == "sendMessage"]

    def answers(self):
        return [d["text"] for m, d in self.calls if m == "answerCallbackQuery"]


def msg(uid, text, chat_type="private", chat_id=None, **extra):
    return {"message": {"message_id": 7, "date": 1700000000, "chat": {"id": chat_id or uid, "type": chat_type},
                        "from": {"id": uid, "first_name": "U"}, "text": text, **extra}}


def cb(uid, data, n="1"):
    return {"callback_query": {"id": n, "from": {"id": uid}, "data": data, "message": {"chat": {"id": uid}}}}


@pytest.fixture
def admin(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "admin_telegram_ids", "1")
    monkeypatch.setattr(s, "data_dir", tmp_path)


def make_bot(sm, connector):
    fake = Fake()
    orch = Orchestrator(connector, HybridSearcher(HashEmbedder()), notifier=LogNotifier())
    return GlassBot(TelegramBotAPI("1:T", transport=httpx.MockTransport(fake)), sm, orch), fake


async def test_admin_setup_without_code(pg, admin):
    _, sm = pg
    bot, fake = make_bot(sm, MockConnector())
    await bot.handle(msg(5, "/newcompany Тест"))
    assert "администратор" in fake.texts()[-1]
    await bot.handle(msg(1, "/newcompany Вторая компания"))
    await bot.handle(msg(1, "/setchannel telegram @test_channel"))
    await bot.handle(msg(1, "/addsource website https://example.com конкурент"))
    await bot.handle(msg(1, "/invite 5 approver"))
    async with sm() as s:
        c = await s.scalar(select(Company))
        assert c.settings["channels"]["telegram"] == "@test_channel"
        roles = (await s.execute(select(User.telegram_id, CompanyMember.role).join(
            CompanyMember, CompanyMember.user_id == User.id))).all()
        assert set(roles) == {(1, "owner"), (5, "approver")}
    await bot.handle(msg(5, "/companies"))
    assert "Вторая компания (approver)" in fake.texts()[-1]
    await bot.handle(msg(5, "/tasks"))
    assert "Еженедельный отчёт" in fake.texts()[-1]


async def test_chat_approval_and_revision(pg, admin):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "Салаватстекло", slug="sal")
        owner, viewer = User(telegram_id=10, prefs={}), User(telegram_id=20, prefs={})
        s.add_all([owner, viewer])
        await s.flush()
        s.add_all([CompanyMember(company_id=c.id, user_id=owner.id, role="owner"),
                   CompanyMember(company_id=c.id, user_id=viewer.id, role="viewer")])
        post = await P.create_draft(s, c.id, text="Пост")
        await P.submit(s, post)
        pid = post.id
    bot, fake = make_bot(sm, MockConnector([json.dumps({"answer": "Привет из агента"})]))
    await bot.handle(msg(10, "Привет"))  # одна компания — выбирается автоматически
    assert fake.texts()[-1] == "[ТЕСТОВАЯ МОДЕЛЬ] Привет из агента"
    await bot.handle(msg(10, "/posts"))
    assert "На согласование · Telegram" in fake.texts()[-1]

    await bot.handle(cb(20, f"ap:{pid}"))
    assert fake.answers()[-1] == "Недостаточно прав"
    # на доработку: кнопка → комментарий → revision (AI недоступен в моке без ответов → сообщение об ошибке)
    await bot.handle(cb(10, f"rv:{pid}"))
    assert "что доработать" in fake.texts()[-1]
    bot.orchestrator = None
    await bot.handle(msg(10, "Добавь цену"))
    async with sm() as s:
        p = await s.get(Post, pid)
        assert p.status == "revision" and p.review_comment == "Добавь цену"
        await P.edit_content(s, p, text="Пост с ценой")
        await P.submit(s, p)
        await s.commit()
    await bot.handle(cb(10, f"pn:{pid}", "2"))
    assert fake.answers()[-1].startswith("Согласовано")
    async with sm() as s:
        p = await s.get(Post, pid)
        assert p.status == "scheduled" and P.is_approved_content(p)


async def test_group_comments_to_inbox(pg, admin):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "К", slug="kk", settings={"channels": {"telegram_discussion": "-100500"}})
        o = User(telegram_id=10, prefs={})
        s.add(o)
        await s.flush()
        s.add(CompanyMember(company_id=c.id, user_id=o.id, role="owner"))
    bot, fake = make_bot(sm, DevMockConnector())
    await bot.handle(msg(77, "Сколько стоит триплекс?", chat_type="supergroup", chat_id=-100500))
    await bot.handle(msg(77, "автопересылка", chat_type="supergroup", chat_id=-100500, is_automatic_forward=True))
    await bot.handle(msg(77, "чужая группа", chat_type="supergroup", chat_id=-1))
    async with sm() as s:
        items = (await s.scalars(select(InboxItem))).all()
        assert [i.text for i in items] == ["Сколько стоит триплекс?"]
        iid = items[0].id
    await bot.handle(cb(10, f"id:{iid}"))
    assert fake.answers()[-1] == "Черновик готов — /inbox"
    await bot.handle(cb(10, f"is:{iid}", "2"))
    assert fake.answers()[-1] == "Ответ отправлен"
    sent = [d for m, d in fake.calls if m == "sendMessage" and d.get("chat_id") == "-100500"][0]
    assert sent["reply_parameters"] == {"message_id": 7} and sent["text"].startswith("[ТЕСТОВАЯ МОДЕЛЬ]")
