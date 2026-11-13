"""Публикация в Telegram (мок Bot API через httpx.MockTransport) и задания по расписанию.

Реальный Telegram не вызывается: тесты проверяют логику, а не доступ бота к каналу."""
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db.models import Memory, Post
from app.services import posts as P
from app.services.publisher import LogNotifier, TelegramBotAPI, TelegramPublisher, run_due
from app.services.sources import create_company


class FakeTelegram:
    def __init__(self, fail_first: int = 0, permanent: bool = False):
        self.calls = []
        self.fail_first = fail_first
        self.permanent = permanent

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, request))
        if self.fail_first:
            self.fail_first -= 1
            if self.permanent:
                return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "chat not found"})
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests",
                                             "parameters": {"retry_after": 5}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 100 + len(self.calls),
                                                                 "chat": {"id": -1001, "username": "salstek"}}})


def make(fake):
    api = TelegramBotAPI("123:TEST", transport=httpx.MockTransport(fake))
    return TelegramPublisher(api), LogNotifier()


async def scheduled_post(sm, *, at=None, text="Текст поста", channel="@salstek"):
    async with sm() as s, s.begin():
        c = await create_company(s, "Компания", slug="c")
        c.settings = {"telegram_channel": channel}
        p = await P.create_draft(s, c.id, text=text, topic="Тема")
        await P.approve(s, p, user_id=None, scheduled_at=at or datetime.now(timezone.utc) - timedelta(seconds=1))
        return c.id, p.id


async def test_scheduled_post_published_once(pg):
    _, sm = pg
    cid, pid = await scheduled_post(sm)
    fake = FakeTelegram()
    pub, notifier = make(fake)
    assert await run_due(sm, pub, notifier) == {str(pid): "published"}
    assert await run_due(sm, pub, notifier) == {}  # повторный проход ничего не отправляет
    assert len(fake.calls) == 1 and fake.calls[0][0] == "sendMessage"
    assert json.loads(fake.calls[0][1].content) == {"chat_id": "@salstek", "text": "Текст поста"}
    async with sm() as s:
        p = await s.get(Post, pid)
        assert p.status == "published" and p.external_message_id == "101"
        assert p.external_url == "https://t.me/salstek/101"
        assert await s.scalar(select(Memory).where(Memory.category == "post_published"))
    assert "Опубликован" in notifier.sent[0][1]


async def test_future_post_waits_and_concurrent_claim(pg):
    _, sm = pg
    _, pid = await scheduled_post(sm, at=datetime.now(timezone.utc) + timedelta(hours=1))
    fake = FakeTelegram()
    pub, notifier = make(fake)
    assert await run_due(sm, pub, notifier) == {}
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    async with sm() as s, s.begin():
        assert await P.claim_for_publishing(s, pid)
    async with sm() as s, s.begin():
        assert not await P.claim_for_publishing(s, pid)  # второй воркер не получит тот же пост
    assert await run_due(sm, pub, notifier, now=later) == {}
    assert fake.calls == []


async def test_rate_limit_retry_then_publish(pg):
    _, sm = pg
    _, pid = await scheduled_post(sm)
    fake = FakeTelegram(fail_first=1)
    pub, notifier = make(fake)
    assert await run_due(sm, pub, notifier) == {str(pid): "scheduled"}
    async with sm() as s:
        p = await s.get(Post, pid)
        assert p.publish_attempts == 1 and "429" in p.last_error
        retry_at = p.scheduled_at
    assert await run_due(sm, pub, notifier, now=retry_at + timedelta(seconds=1)) == {str(pid): "published"}


async def test_permanent_error_marks_failed_and_notifies(pg):
    _, sm = pg
    _, pid = await scheduled_post(sm)
    pub, notifier = make(FakeTelegram(fail_first=1, permanent=True))
    assert await run_due(sm, pub, notifier) == {str(pid): "failed"}
    assert "не опубликован" in notifier.sent[0][1]


async def test_unapproved_post_never_published(pg):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "Компания", slug="c")
        c.settings = {"telegram_channel": "@x"}
        p = await P.create_draft(s, c.id, text="Черновик")
        # обходим state machine напрямую — публикация всё равно должна отказать
        p.status, p.scheduled_at = "scheduled", datetime.now(timezone.utc)
        pid = p.id
    fake = FakeTelegram()
    pub, notifier = make(fake)
    await run_due(sm, pub, notifier)
    assert fake.calls == []
    async with sm() as s:
        assert (await s.get(Post, pid)).status == "draft"


async def test_photo_post(pg, tmp_path):
    _, sm = pg
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG fake")
    async with sm() as s, s.begin():
        c = await create_company(s, "Компания", slug="c")
        p = await P.create_draft(s, c.id, text="С картинкой")
        await P.edit_content(s, p, image_path=str(img), image_meta={"generator": "pillow_template"})
        await P.approve(s, p, user_id=None, target_chat_id="-1001",
                        scheduled_at=datetime.now(timezone.utc))
    fake = FakeTelegram()
    pub, notifier = make(fake)
    await run_due(sm, pub, notifier)
    assert fake.calls[0][0] == "sendPhoto" and b"caption" in fake.calls[0][1].content
