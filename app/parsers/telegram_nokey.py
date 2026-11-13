"""Telegram без API-ключей.

* `TelegramWebPreviewParser` — открытые каналы через официальное веб-превью
  https://t.me/s/<username> (то, что видит любой посетитель сайта). Для
  закрытых каналов (ссылки t.me/+...) Telegram превью не отдаёт.
* `TelegramExportImporter` — закрытые каналы через встроенную функцию
  Telegram Desktop «Экспорт истории чата» (формат JSON). Экспорт делает
  участник канала у себя в приложении; ключи и вход в аккаунт не нужны.

Документы совместимы с остальными парсерами (ParsedDocument), дубли
исключаются по ключу канал+пост, синхронизация инкрементальная.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .base import (
    IndexCallback,
    JsonlDocumentStore,
    ParsedDocument,
    SourceType,
    SyncResult,
    UpsertStatus,
    clean_text,
    extract_hashtags,
    make_title,
    utcnow,
)
from .net import DEFAULT_USER_AGENT
from .telegram_parser import TelegramAccessError, parse_channel_ref

log = logging.getLogger("glass.telegram_web")
_COUNT_RE = re.compile(r"([\d.,]+)\s*([KMB]?)", re.I)


def parse_count(text: str | None) -> int | None:
    """'480K' → 480000, '24.5K' → 24500, '1.2M' → 1200000, '935' → 935."""
    if not text:
        return None
    m = _COUNT_RE.search(text.replace(" ", " "))
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(round(num * mult))


async def _store_docs(docs: list[ParsedDocument], store: JsonlDocumentStore, result: SyncResult,
                      changed: list[ParsedDocument]) -> None:
    for doc in docs:
        status = store.upsert(doc)
        result.count(status)
        if status in (UpsertStatus.NEW, UpsertStatus.UPDATED):
            changed.append(doc)


# ============================================================================ веб-превью


WEB_MEDIA_CLASSES = {
    "tgme_widget_message_photo_wrap": "photo",
    "tgme_widget_message_video_player": "video",
    "tgme_widget_message_roundvideo_player": "video_note",
    "tgme_widget_message_voice_player": "voice",
    "tgme_widget_message_document_wrap": "document",
    "tgme_widget_message_poll": "poll",
    "tgme_widget_message_sticker_wrap": "sticker",
    "tgme_widget_message_link_preview": "link_preview",
    "tgme_widget_message_location_wrap": "location",
}


class TelegramWebStructureError(RuntimeError):
    """Страница загрузилась, но её разметка не похожа на веб-превью канала (Telegram изменил HTML)."""


@dataclass
class WebPage:
    info: dict[str, Any]
    posts: list[dict[str, Any]]
    before: int | None  # id для запроса более старой страницы (?before=)
    warnings: list[str] = field(default_factory=list)


def parse_web_preview(html: str) -> WebPage:
    """Разбирает страницу t.me/s/<channel>.

    TelegramAccessError — это страница приглашения закрытого канала (постов нет);
    TelegramWebStructureError — нет ни блока канала, ни постов (разметка изменилась)."""
    soup = BeautifulSoup(html, "lxml")
    has_channel = soup.select_one(".tgme_channel_info") is not None
    wraps = soup.select(".tgme_widget_message_wrap, .tgme_widget_message[data-post]")
    if not has_channel and not wraps:
        if soup.select_one(".tgme_page_title") and soup.select_one('a[href^="tg://join"]'):
            title = clean_text(soup.select_one(".tgme_page_title").get_text(" "))
            raise TelegramAccessError(
                f"«{title}» — закрытый канал: у ссылок-приглашений t.me/+… нет веб-страницы с постами")
        raise TelegramWebStructureError("на странице нет ни .tgme_channel_info, ни .tgme_widget_message — "
                                        "похоже, Telegram изменил разметку веб-превью")
    info: dict[str, Any] = {}
    if t := soup.select_one(".tgme_channel_info_header_title"):
        info["title"] = clean_text(t.get_text(" "))
    if d := soup.select_one(".tgme_channel_info_description"):
        info["description"] = clean_text(d.get_text("\n"))
    for c in soup.select(".tgme_channel_info_counter"):
        val, typ = c.select_one(".counter_value"), c.select_one(".counter_type")
        if val and typ:
            info[clean_text(typ.get_text())] = parse_count(val.get_text())

    posts: list[dict[str, Any]] = []
    warnings: list[str] = []
    for m in soup.select(".tgme_widget_message"):
        data_post = m.get("data-post") or ""
        channel, _, pid = data_post.rpartition("/")
        if not channel or not pid.isdigit():
            warnings.append(f"пост без корректного data-post ({data_post!r}) пропущен — проверьте разметку")
            continue
        text_el = m.select_one(".tgme_widget_message_text")
        if text_el:
            for br in text_el.find_all("br"):
                br.replace_with("\n")
        text = clean_text(text_el.get_text()) if text_el else ""
        time_el = m.select_one(".tgme_widget_message_date time[datetime]")
        date = None
        if time_el is not None:
            try:
                date = datetime.fromisoformat(time_el["datetime"])
            except ValueError:
                warnings.append(f"{data_post}: не удалось разобрать дату {time_el['datetime']!r}")
        else:
            warnings.append(f"{data_post}: нет даты публикации")
        reactions: dict[str, int] = {}
        for i, r in enumerate(m.select(".tgme_reaction")):
            emoji = r.select_one("i.emoji b, b")
            custom = r.select_one("tg-emoji[emoji-id]")
            if "tgme_reaction_paid" in (r.get("class") or []):
                key = "paid"
            elif emoji and clean_text(emoji.get_text()):
                key = clean_text(emoji.get_text())
            elif custom is not None:
                key = f"custom:{custom['emoji-id']}"
            else:
                key = f"reaction_{i}"
            count = parse_count(r.get_text(" ").replace(emoji.get_text() if emoji else "", ""))
            if count:
                reactions[key] = reactions.get(key, 0) + count
        media = []
        for cls, kind in WEB_MEDIA_CLASSES.items():
            media += [kind] * len(m.select(f".{cls}"))
        if m.select_one(".message_media_not_supported") and not media:
            media.append("unsupported")  # превью показывает «откройте в Telegram»
        fwd = m.select_one(".tgme_widget_message_forwarded_from")
        views_el = m.select_one(".tgme_widget_message_views")
        meta_el = m.select_one(".tgme_widget_message_meta")
        author_el = m.select_one(".tgme_widget_message_from_author")
        posts.append({
            "channel": channel,
            "id": int(pid),
            "text": text,
            "date": date,
            "edited": bool(meta_el and "edited" in meta_el.get_text()),
            "views": parse_count(views_el.get_text()) if views_el else None,
            "reactions": reactions,
            "media": media,
            "forwarded_from": clean_text(fwd.get_text(" ")).removeprefix("Forwarded from").strip() if fwd else None,
            "author": clean_text(author_el.get_text()) if author_el else None,
        })
    more = soup.select_one("a.tme_messages_more[data-before], a.tme_messages_more[href*='before=']")
    before = None
    if more is not None:
        raw = more.get("data-before")
        if not raw and (mm := re.search(r"before=(\d+)", more.get("href", ""))):
            raw = mm.group(1)
        before = int(raw) if raw and str(raw).isdigit() else None
    elif posts:
        before = min(p["id"] for p in posts)
    return WebPage(info, posts, before, warnings)


def web_post_to_document(p: dict[str, Any], channel_title: str) -> ParsedDocument:
    """Пост → документ. Посты без текста (только фото/видео или пустые) тоже сохраняются."""
    reactions = p["reactions"]
    return ParsedDocument(
        source_type=SourceType.TELEGRAM,
        source_id=p["channel"].lower(),
        external_id=str(p["id"]),
        url=f"https://t.me/{p['channel']}/{p['id']}",
        title=make_title(p["text"], p["media"]) if (p["text"] or p["media"]) else "[пост без текста]",
        text=p["text"],
        published_at=p["date"],
        metrics={"views": p["views"], "reactions": reactions, "reactions_total": sum(reactions.values()),
                 "approximate": True},  # превью округляет большие числа (24.5K)
        metadata={"channel_title": channel_title, "channel_username": p["channel"], "post_id": p["id"],
                  "message_ids": [p["id"]], "has_text": bool(p["text"]),
                  "hashtags": extract_hashtags(p["text"]), "media_types": p["media"], "media_count": len(p["media"]),
                  "is_forward": bool(p["forwarded_from"]), "forwarded_from": p["forwarded_from"],
                  "post_author": p["author"], "edited": p["edited"], "via": "web_preview"},
    )


class TelegramWebPreviewParser:
    """Сбор постов открытого канала со страницы https://t.me/s/<имя> (HTTP + разбор HTML).

    * первый запуск: посты с текущей страницы (initial_pages страниц, по умолчанию 1);
    * далее: все новые посты (листает ?before= до уже известного id), сохраняет от старых к новым;
    * ошибка запроса или изменившаяся разметка: причина в лог и в состояние, позиция
      last_message_id остаётся от последней успешной обработки — ничего не теряется.
    """

    BASE = "https://t.me/s/"

    def __init__(self, *, delay: float = 1.0, timeout: float = 20, retry_base: float = 1.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.delay = delay
        self.timeout = timeout
        self.retry_base = retry_base
        self._transport = transport

    async def _get(self, client: httpx.AsyncClient, url: str, attempts: int = 4) -> httpx.Response:
        """GET с повторами при таймаутах, 429 и 5xx (нарастающая пауза)."""
        r: httpx.Response | None = None
        for i in range(attempts):
            try:
                r = await client.get(url)
                if r.status_code != 429 and r.status_code < 500:
                    return r
            except httpx.TransportError:
                if i == attempts - 1:
                    raise
            await asyncio.sleep(min(2 ** i * self.retry_base, 30))
        assert r is not None
        return r

    async def _collect(self, client: httpx.AsyncClient, username: str, last_id: int, first_run: bool,
                       initial_pages: int, max_posts: int, warnings: list[str]) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
        info: dict[str, Any] = {}
        collected: dict[int, dict[str, Any]] = {}
        before: int | None = None
        pages = 0
        while True:
            url = self.BASE + username + (f"?before={before}" if before else "")
            r = await self._get(client, url)
            if r.status_code in (301, 302, 303, 307, 308):
                raise TelegramAccessError(f"t.me перенаправляет {url} → {r.headers.get('location')}: "
                                          "у канала нет веб-превью (закрытый канал или не канал)")
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code} для {url}", request=r.request, response=r)
            page = parse_web_preview(r.text)
            warnings.extend(page.warnings)
            info = info or page.info
            pages += 1
            for p in page.posts:
                collected.setdefault(p["id"], p)
            if not page.posts:
                break
            min_id = min(p["id"] for p in page.posts)
            if first_run:
                if pages >= initial_pages:
                    break
            elif min_id <= last_id:
                break  # дошли до уже сохранённых постов
            if len(collected) >= max_posts:
                if not first_run and min_id > last_id + 1:
                    warnings.append(f"достигнут лимит max_posts={max_posts}: посты с id {last_id + 1}…{min_id - 1} "
                                    "не собраны за этот проход")
                break
            if not page.before or page.before <= 1 or page.before == before:
                break
            before = page.before
            await asyncio.sleep(self.delay)
        return info, collected

    async def sync(self, ref: str, store: JsonlDocumentStore, *, max_posts: int = 1000,
                   initial_pages: int = 1, index_callback: IndexCallback | None = None) -> SyncResult:
        kind, username = parse_channel_ref(ref)
        if kind != "username":
            raise TelegramAccessError(
                f"{ref}: это ссылка-приглашение закрытого канала — у него нет веб-страницы t.me/s/ с постами. "
                "Укажите открытый канал (t.me/<имя>) или импортируйте экспорт Telegram Desktop (telegram-export).")
        channel = username.lower()
        state_key = f"telegram_web:{channel}"
        state = store.get_state(state_key)
        last_id = int(state.get("last_message_id", 0))
        first_run = last_id == 0
        result = SyncResult(source_type=SourceType.TELEGRAM, source_id=channel)
        changed: list[ParsedDocument] = []
        warnings: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, transport=self._transport,
                                         headers={"User-Agent": DEFAULT_USER_AGENT}) as client:
                info, collected = await self._collect(client, username, last_id, first_run, initial_pages,
                                                      max_posts, warnings)
        except TelegramAccessError:
            raise  # ошибка настройки (закрытый канал) — повтор не поможет
        except (httpx.HTTPError, TelegramWebStructureError) as e:
            reason = f"{type(e).__name__}: {e}"
            log.error("@%s: проход прерван, позиция сохранена (последний пост %s). Причина: %s",
                      channel, last_id or "—", reason)
            store.set_state(state_key, {**state, "last_error": reason, "last_error_at": utcnow().isoformat()})
            store.save()
            result.errors.append(reason)
            result.finished_at = utcnow()
            return result
        for w in warnings:
            log.warning("@%s: %s", channel, w)

        title = info.get("title") or state.get("title") or username
        new_posts = sorted((p for pid, p in collected.items() if pid > last_id), key=lambda p: p["id"])
        for p in new_posts:  # новые — от старых к новым
            await _store_docs([web_post_to_document(p, title)], store, result, changed)
        for pid in sorted(i for i in collected if i <= last_id):  # уже сохранённые: обновить просмотры/реакции
            doc = web_post_to_document(collected[pid], title)
            if store.get(doc.key) is not None:  # более старые, никогда не сохранявшиеся посты не догружаем
                await _store_docs([doc], store, result, changed)
        max_id = max([last_id] + [p["id"] for p in new_posts])
        store.set_state(state_key, {
            "ref": ref, "channel": channel, "last_message_id": max_id, "last_sync": utcnow().isoformat(),
            "last_error": None, "title": title, **{k: v for k, v in info.items() if k != "title"},
        })
        store.save()
        result.notes.append(
            f"канал «{title}» (@{channel}): {'первый запуск, ' if first_run else ''}новых постов {len(new_posts)}"
            + (f" (id {new_posts[0]['id']}…{new_posts[-1]['id']})" if new_posts else "")
            + f", последний сохранённый id {max_id}")
        if warnings:
            result.notes.append(f"предупреждений разметки: {len(warnings)} (см. лог)")
        log.info("@%s: новых %d, обновлено %d, последний id %s", channel, result.new, result.updated, max_id)
        if index_callback and changed:
            await index_callback(changed)
        result.finished_at = utcnow()
        return result


# ============================================================================ экспорт Telegram Desktop


def _export_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(v if isinstance(v, str) else str(v.get("text", "")) for v in value)
    return ""


def _export_media(m: dict[str, Any]) -> list[str]:
    if "photo" in m:
        return ["photo"]
    mt = m.get("media_type")
    if mt:
        return [{"video_file": "video", "voice_message": "voice", "video_message": "video_note",
                 "audio_file": "audio", "animation": "gif"}.get(mt, mt)]
    if "poll" in m:
        return ["poll"]
    if "file" in m or "mime_type" in m:
        return ["document"]
    if "location_information" in m:
        return ["location"]
    return []


def _export_date(m: dict[str, Any], key: str) -> datetime | None:
    unix = m.get(f"{key}_unixtime")
    if unix:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc)
    if m.get(key):
        dt = datetime.fromisoformat(m[key])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def load_export(path: Path) -> dict[str, Any]:
    """Принимает result.json или папку экспорта (ChatExport_*/result.json)."""
    path = Path(path).expanduser()
    if path.is_dir():
        candidates = sorted(path.rglob("result.json"))
        if not candidates:
            raise FileNotFoundError(f"В папке {path} нет result.json — выберите формат JSON при экспорте")
        path = candidates[0]
    if path.stat().st_size > 500 * 1024 * 1024:
        raise ValueError("Файл экспорта больше 500 МБ — экспортируйте без медиафайлов")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "messages" not in data:
        if isinstance(data, dict) and "chats" in data:
            raise ValueError("Это экспорт всего аккаунта — экспортируйте историю одного канала (⋮ → Экспорт истории)")
        raise ValueError("Неверный формат: ожидается result.json экспорта истории чата Telegram Desktop")
    return data


def export_to_documents(data: dict[str, Any]) -> list[ParsedDocument]:
    channel_id = str(data.get("id", "")).removeprefix("-100")
    title = data.get("name") or channel_id
    groups: list[list[dict[str, Any]]] = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        text = clean_text(_export_text(m.get("text")))
        prev = groups[-1][0] if groups else None
        # альбом: сообщения без текста с той же секундой публикации, что и предыдущее
        if (prev and not text and _export_media(m) and m.get("date_unixtime") == prev.get("date_unixtime")
                and m.get("from_id") == prev.get("from_id")):
            groups[-1].append(m)
        else:
            groups.append([m])

    docs = []
    for parts in groups:
        first = parts[0]
        text = clean_text("\n\n".join(t for t in (clean_text(_export_text(p.get("text"))) for p in parts) if t))
        media = [x for p in parts for x in _export_media(p)]
        if not text and not media:
            continue
        reactions: dict[str, int] = {}
        for p in parts:
            for r in p.get("reactions") or []:
                key = r.get("emoji") or (f"custom:{r.get('document_id')}" if r.get("document_id") else r.get("type", "?"))
                reactions[key] = reactions.get(key, 0) + int(r.get("count", 0))
        tags = [e["text"].lstrip("#").lower() for p in parts for e in p.get("text_entities") or []
                if e.get("type") == "hashtag" and e.get("text")]
        mid = first["id"]
        docs.append(ParsedDocument(
            source_type=SourceType.TELEGRAM,
            source_id=channel_id,
            external_id=str(mid),
            url=f"https://t.me/c/{channel_id}/{mid}",
            title=make_title(text, media),
            text=text,
            published_at=_export_date(first, "date"),
            edited_at=max((d for d in (_export_date(p, "edited") for p in parts) if d), default=None),
            metrics={"reactions": reactions, "reactions_total": sum(reactions.values()),
                     "views": None},  # экспорт Telegram Desktop не содержит просмотров
            metadata={"channel_title": title, "channel_username": None, "message_ids": [p["id"] for p in parts],
                      "hashtags": list(dict.fromkeys(tags)) or extract_hashtags(text), "media_types": media,
                      "media_count": len(media), "is_forward": bool(first.get("forwarded_from")),
                      "forwarded_from": first.get("forwarded_from"), "post_author": first.get("author"),
                      "via": "desktop_export"},
        ))
    return docs


class TelegramExportImporter:
    async def sync(self, path: str | Path, store: JsonlDocumentStore,
                   index_callback: IndexCallback | None = None) -> SyncResult:
        data = await asyncio.to_thread(load_export, Path(path))
        docs = export_to_documents(data)
        channel_id = str(data.get("id", "")).removeprefix("-100")
        result = SyncResult(source_type=SourceType.TELEGRAM, source_id=channel_id)
        changed: list[ParsedDocument] = []
        await _store_docs(docs, store, result, changed)
        dates = [d.published_at for d in docs if d.published_at]
        store.set_state(f"telegram_export:{channel_id}", {
            "file": str(path), "title": data.get("name"), "type": data.get("type"),
            "last_message_id": max((int(d.external_id) for d in docs), default=0),
            "imported_at": utcnow().isoformat(),
        })
        result.notes.append(f"экспорт «{data.get('name')}» ({data.get('type')}): постов {len(docs)}"
                            + (f", с {min(dates):%d.%m.%Y} по {max(dates):%d.%m.%Y}" if dates else "")
                            + "; просмотры в экспорте Telegram отсутствуют")
        store.save()
        if index_callback and changed:
            await index_callback(changed)
        result.finished_at = utcnow()
        return result
