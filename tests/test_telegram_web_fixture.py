"""Сбор открытого канала через t.me/s/ на СОХРАНЁННЫХ страницах (tests/fixtures/telegram_web, сохранены 13.11.2026).

Сеть замокана: MockTransport отдаёт сохранённый HTML вместо t.me.
"""
import logging
from pathlib import Path

import httpx
import pytest

from app.parsers.base import JsonlDocumentStore, UpsertStatus
from app.parsers.telegram_nokey import (
    TelegramWebPreviewParser, TelegramWebStructureError, parse_web_preview, web_post_to_document,
)
from app.parsers.telegram_parser import TelegramAccessError
from app.services.sources import normalize_source_ref

FIX = Path(__file__).parent / "fixtures" / "telegram_web"
PAGE1 = (FIX / "salstek_page1.html").read_text(encoding="utf-8")  # посты 1114…1143, before=1114
PAGE2 = (FIX / "salstek_page2.html").read_text(encoding="utf-8")  # посты 1088…1113
INVITE = (FIX / "private_invite.html").read_text(encoding="utf-8")  # t.me/+5LuOqWJkZDU0N2Ri/s/10
P1_IDS = sorted(p["id"] for p in parse_web_preview(PAGE1).posts)
P2_IDS = sorted(p["id"] for p in parse_web_preview(PAGE2).posts)


class SpyStore(JsonlDocumentStore):
    """Хранилище проекта + запись порядка сохранения новых постов."""
    def __init__(self, d):
        super().__init__(d)
        self.new_order = []

    def upsert(self, doc):
        st = super().upsert(doc)
        if st is UpsertStatus.NEW:
            self.new_order.append(int(doc.external_id))
        return st


def site(pages: dict, calls: list | None = None):
    def handler(req: httpx.Request):
        if calls is not None:
            calls.append(str(req.url))
        body = pages.get(req.url.params.get("before"))
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)
    return httpx.MockTransport(handler)


def parser(transport):
    return TelegramWebPreviewParser(delay=0, retry_base=0, transport=transport)


def test_saved_page_has_many_posts_with_required_fields():
    page = parse_web_preview(PAGE1)
    assert page.info["title"] == "САЛАВАТСТЕКЛО" and not page.warnings
    assert len(page.posts) >= 10
    for p in page.posts:
        d = web_post_to_document(p, page.info["title"])
        assert d.external_id == str(p["id"]) and d.published_at is not None
        assert d.url == f"https://t.me/salstek/{p['id']}"
    assert any(not p["text"] for p in page.posts)  # посты без текста (только фото) на странице есть


async def test_first_run_saves_current_page_and_rerun_has_no_duplicates(tmp_path):
    calls = []
    t = site({None: PAGE1, "1114": PAGE2}, calls)
    store = SpyStore(tmp_path)
    r1 = await parser(t).sync("salstek", store)
    assert r1.new == len(P1_IDS) and not r1.errors
    assert calls == ["https://t.me/s/salstek"]  # первый запуск — только текущая страница
    assert store.new_order == P1_IDS  # от старых к новым
    no_text = [d for d in store.all() if not d.text]
    assert no_text and all(d.title == "[photo]" or d.title.startswith("[") for d in no_text)  # без текста — сохранены

    store2 = SpyStore(tmp_path)
    r2 = await parser(t).sync("https://t.me/salstek", store2)  # та же страница, другая запись имени канала
    assert r2.new == 0 and r2.updated == 0 and len(store2) == len(P1_IDS)
    keys = [d.key for d in store2.all()]
    assert len(keys) == len(set(keys))
    assert store2.get_state("telegram_web:salstek")["last_message_id"] == P1_IDS[-1]


async def test_new_posts_across_pages_saved_oldest_first(tmp_path):
    # первый запуск видит только старую страницу (как будто это было раньше)
    await parser(site({None: PAGE2})).sync("salstek", SpyStore(tmp_path))
    # теперь на канале появилась целая страница новых постов: парсер листает назад до известного id
    calls = []
    store = SpyStore(tmp_path)
    r = await parser(site({None: PAGE1, "1114": PAGE2}, calls)).sync("salstek", store)
    assert r.new == len(P1_IDS) and store.new_order == P1_IDS
    assert len(calls) == 2  # дошёл до сохранённых постов и остановился
    assert len(store) == len(P1_IDS) + len(P2_IDS)
    assert store.get_state("telegram_web:salstek")["last_message_id"] == max(P1_IDS)


async def test_network_error_keeps_position_and_logs(tmp_path, caplog):
    await parser(site({None: PAGE2})).sync("salstek", JsonlDocumentStore(tmp_path))
    caplog.set_level(logging.ERROR, logger="glass.telegram_web")
    store = JsonlDocumentStore(tmp_path)
    r = await parser(site({None: PAGE1, "1114": httpx.ConnectTimeout("timeout")})).sync("salstek", store)
    assert r.errors and "ConnectTimeout" in r.errors[0] and r.new == 0
    state = JsonlDocumentStore(tmp_path).get_state("telegram_web:salstek")
    assert state["last_message_id"] == max(P2_IDS) and "ConnectTimeout" in state["last_error"]
    assert len(JsonlDocumentStore(tmp_path)) == len(P2_IDS)  # частичных записей нет
    assert "позиция сохранена" in caplog.text and "ConnectTimeout" in caplog.text
    # следующий успешный проход догоняет всё пропущенное
    r2 = await parser(site({None: PAGE1, "1114": PAGE2})).sync("salstek", JsonlDocumentStore(tmp_path))
    assert r2.new == len(P1_IDS) and JsonlDocumentStore(tmp_path).get_state("telegram_web:salstek")["last_error"] is None


async def test_html_structure_change_logged(tmp_path, caplog):
    await parser(site({None: PAGE2})).sync("salstek", JsonlDocumentStore(tmp_path))
    caplog.set_level(logging.ERROR, logger="glass.telegram_web")
    changed = "<html><body><div class='new_layout'>Посты</div></body></html>"
    r = await parser(site({None: changed})).sync("salstek", JsonlDocumentStore(tmp_path))
    assert r.errors and "TelegramWebStructureError" in r.errors[0]
    assert JsonlDocumentStore(tmp_path).get_state("telegram_web:salstek")["last_message_id"] == max(P2_IDS)
    assert "изменил разметку" in caplog.text
    with pytest.raises(TelegramWebStructureError):
        parse_web_preview(changed)


def test_post_without_data_post_is_warned():
    broken = PAGE1.replace('data-post="salstek/1143"', 'data-post=""', 1)
    page = parse_web_preview(broken)
    assert 1143 not in {p["id"] for p in page.posts} and page.warnings


async def test_private_invite_page_is_explained(tmp_path):
    with pytest.raises(TelegramAccessError, match="закрытый канал"):
        parse_web_preview(INVITE)
    with pytest.raises(TelegramAccessError):
        await parser(site({None: INVITE})).sync("https://t.me/+5LuOqWJkZDU0N2Ri/s/10", JsonlDocumentStore(tmp_path))


def test_one_channel_one_source():
    refs = ["salstek", "@salstek", "https://t.me/salstek", "https://t.me/s/salstek", "t.me/SalStek"]
    assert {normalize_source_ref("telegram_web", r) for r in refs} == {"https://t.me/salstek"}
