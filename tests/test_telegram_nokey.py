"""Telegram без ключей: веб-превью (страницы ЗАМОКАНЫ) и импорт экспорта Telegram Desktop."""
import json

import pytest

from app.parsers.base import JsonlDocumentStore
from app.parsers.telegram_nokey import (
    TelegramExportImporter, TelegramWebPreviewParser, export_to_documents, parse_count, parse_web_preview,
)
from app.parsers.telegram_parser import TelegramAccessError
from scripts.vk_token import extract_token


def post_html(pid, text, views="1.2K", reactions='<span class="tgme_reaction"><i class="emoji"><b>🔥</b></i>5</span>'
              '<span class="tgme_reaction"><tg-emoji emoji-id="777"></tg-emoji>2</span>', photo=True):
    ph = '<a class="tgme_widget_message_photo_wrap"></a>' if photo else ""
    return (f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="chan/{pid}">{ph}'
            f'<div class="tgme_widget_message_text">{text}</div>'
            f'<div class="tgme_widget_message_reactions">{reactions}</div>'
            f'<span class="tgme_widget_message_views">{views}</span>'
            f'<a class="tgme_widget_message_date"><time datetime="2026-11-0{pid % 9 + 1}T10:00:00+00:00"></time></a>'
            f'</div></div>')


def page(ids, before=None):
    more = f'<a class="tme_messages_more" data-before="{before}" href="/s/chan?before={before}"></a>' if before else ""
    info = ('<div class="tgme_channel_info_header_title">Канал</div>'
            '<div class="tgme_channel_info_counter"><span class="counter_value">1.16K</span>'
            '<span class="counter_type">subscribers</span></div>')
    return f"<html><body>{info}{more}{''.join(post_html(i, f'Пост {i} #стекло') for i in ids)}</body></html>"


def test_parse_count():
    assert (parse_count("480K"), parse_count("24.5K"), parse_count("1.2M"), parse_count("935")) == (480000, 24500, 1200000, 935)


def test_parse_web_preview_synthetic():
    page_ = parse_web_preview(page([5, 6], before=5))
    assert page_.info == {"title": "Канал", "subscribers": 1160} and page_.before == 5
    p = page_.posts[0]
    assert p["views"] == 1200 and p["reactions"] == {"🔥": 5, "custom:777": 2} and p["media"] == ["photo"]


async def test_web_preview_rejects_private_link(tmp_path):
    with pytest.raises(TelegramAccessError):
        await TelegramWebPreviewParser().sync("https://t.me/+5LuOqWJkZDU0N2Ri", JsonlDocumentStore(tmp_path))


EXPORT = {
    "name": "В команде I «СалаватСтекло»", "type": "private_channel", "id": 1234567890,
    "messages": [
        {"id": 1, "type": "service", "date": "2026-01-01T10:00:00", "action": "create_channel"},
        {"id": 2, "type": "message", "date": "2026-01-02T10:00:00", "date_unixtime": "1767348000", "from_id": "channel1",
         "text": ["Поздравляем коллег! ", {"type": "hashtag", "text": "#команда"}],
         "text_entities": [{"type": "plain", "text": "Поздравляем коллег! "}, {"type": "hashtag", "text": "#команда"}],
         "photo": "photos/1.jpg", "reactions": [{"type": "emoji", "count": 7, "emoji": "❤"}]},
        {"id": 3, "type": "message", "date": "2026-01-02T10:00:00", "date_unixtime": "1767348000", "from_id": "channel1",
         "text": "", "photo": "photos/2.jpg"},
        {"id": 4, "type": "message", "date": "2026-01-03T10:00:00", "date_unixtime": "1767434400", "from_id": "channel1",
         "text": "Видео с завода", "media_type": "video_file", "edited_unixtime": "1767440000"},
    ],
}


def test_export_to_documents():
    docs = export_to_documents(EXPORT)
    assert [d.external_id for d in docs] == ["2", "4"]  # служебное пропущено, фото 3 — часть альбома поста 2
    a, b = docs
    assert a.url == "https://t.me/c/1234567890/2" and a.metadata["message_ids"] == [2, 3]
    assert a.metadata["media_types"] == ["photo", "photo"] and a.metadata["hashtags"] == ["команда"]
    assert a.metrics["reactions"] == {"❤": 7} and a.metrics["views"] is None
    assert b.metadata["media_types"] == ["video"] and b.edited_at is not None


async def test_export_importer_folder_and_dedup(tmp_path):
    folder = tmp_path / "ChatExport_2026-11-13"
    folder.mkdir()
    (folder / "result.json").write_text(json.dumps(EXPORT, ensure_ascii=False), encoding="utf-8")
    store = JsonlDocumentStore(tmp_path / "store")
    r1 = await TelegramExportImporter().sync(folder, store)
    r2 = await TelegramExportImporter().sync(folder / "result.json", JsonlDocumentStore(tmp_path / "store"))
    assert (r1.new, r2.new, r2.unchanged) == (2, 0, 2)


async def test_export_rejects_wrong_file(tmp_path):
    (tmp_path / "result.json").write_text('{"chats": {}}')
    with pytest.raises(ValueError, match="одного канала"):
        await TelegramExportImporter().sync(tmp_path, JsonlDocumentStore(tmp_path / "s"))


def test_vk_extract_token():
    tok, params = extract_token("https://oauth.vk.com/blank.html#access_token=vk1.a.ABC_def-123&expires_in=0&user_id=42")
    assert tok == "vk1.a.ABC_def-123" and params["user_id"] == "42"
    assert extract_token("a" * 85)[0] == "a" * 85
    with pytest.raises(ValueError, match="access_denied"):
        extract_token("https://oauth.vk.com/blank.html#error=access_denied&error_description=user+denied")
    with pytest.raises(ValueError):
        extract_token("привет")
