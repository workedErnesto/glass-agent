"""Тесты Telegram-парсера на поддельных объектах сообщений (MOCK, без сети и без аккаунта Telegram)."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import httpx
import pytest

from app.parsers.base import JsonlDocumentStore
from app.parsers.telegram_parser import (
    TelegramBotChannelReader, TelegramConfigError, TelethonChannelParser,
    bot_post_to_document, messages_to_documents, parse_channel_ref,
)

D = datetime(2026, 11, 1, tzinfo=timezone.utc)


def msg(mid, text="", grouped=None, photo=False, views=10, reactions=None, action=None):
    rs = NS(results=[NS(reaction=NS(emoticon=e), count=c) for e, c in (reactions or {}).items()]) if reactions else None
    return NS(id=mid, message=text, grouped_id=grouped, media=object() if photo else None, photo=photo or None,
              views=views, forwards=1, replies=NS(replies=2), reactions=rs, action=action, date=D,
              edit_date=None, fwd_from=None, post_author=None, pinned=False)


def test_parse_channel_ref():
    assert parse_channel_ref("https://t.me/+5LuOqWJkZDU0N2Ri/s/10") == ("invite", "5LuOqWJkZDU0N2Ri")
    assert parse_channel_ref("https://t.me/joinchat/AbCdEf123") == ("invite", "AbCdEf123")
    assert parse_channel_ref("https://t.me/s/durov") == ("username", "durov")
    assert parse_channel_ref("@salstek") == ("username", "salstek")
    assert parse_channel_ref("-1001234567") == ("id", "-1001234567")


def test_messages_to_documents_album_and_metrics():
    msgs = [
        msg(1, "Привет, коллеги! #завод #Команда", reactions={"👍": 5, "🔥": 2}),
        msg(2, "Фото с праздника", grouped=77, photo=True, views=50),
        msg(3, "", grouped=77, photo=True, views=55),
        msg(4, "", action=object()),  # служебное
        msg(5, ""),  # пустое
    ]
    docs = messages_to_documents(msgs, channel_id=123, channel_title="В команде", username=None)
    assert [d.external_id for d in docs] == ["1", "album77"]
    a, b = docs
    assert a.url == "https://t.me/c/123/1"
    assert a.metrics["reactions"] == {"👍": 5, "🔥": 2} and a.metrics["reactions_total"] == 7
    assert a.metadata["hashtags"] == ["завод", "команда"]
    assert b.metadata["media_types"] == ["photo", "photo"] and b.metadata["message_ids"] == [2, 3]
    assert b.metrics["views"] == 55


def test_telethon_requires_credentials():
    with pytest.raises(TelegramConfigError):
        TelethonChannelParser(None, None, "x.session")


def test_bot_post_to_document():
    d = bot_post_to_document({"message_id": 9, "date": 1_700_000_000, "chat": {"id": -1001234, "title": "T"},
                              "caption": "Новый продукт #стекло", "photo": [{}],
                              "caption_entities": [{"type": "hashtag", "offset": 14, "length": 7}]})
    assert d.url == "https://t.me/c/1234/9" and d.metadata["hashtags"] == ["стекло"]
    assert d.metadata["media_types"] == ["photo"]


async def test_bot_reader_offsets_and_dedup(tmp_path):
    sent = []

    def handler(req):
        body = req.read()
        sent.append(body)
        if b'"offset":0' in body.replace(b" ", b""):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 10, "channel_post": {"message_id": 1, "date": 1_700_000_000,
                                                   "chat": {"id": -1005, "username": "ch"}, "text": "Пост"}},
                {"update_id": 11, "edited_channel_post": {"message_id": 1, "date": 1_700_000_000, "edit_date": 1_700_000_100,
                                                          "chat": {"id": -1005, "username": "ch"}, "text": "Пост (ред.)"}},
            ]})
        return httpx.Response(200, json={"ok": True, "result": []})

    store = JsonlDocumentStore(tmp_path)
    res = await TelegramBotChannelReader("T", transport=httpx.MockTransport(handler)).sync(store)
    assert (res.new, res.updated) == (1, 1)
    assert store.get("telegram:5:1").text == "Пост (ред.)" and store.get_state("telegram_bot")["offset"] == 12
