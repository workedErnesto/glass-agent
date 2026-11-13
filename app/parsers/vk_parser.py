"""Парсер сообществ ВКонтакте через официальный VK API.

Методы: groups.getById (профиль сообщества), wall.get (публикации).
Собираются: тексты, даты, лайки, комментарии, репосты, просмотры, медиа,
репосты чужих записей. Обработка rate limit (ошибки 6, 9, 29), ошибок
авторизации (5) и доступа (15, 18, 30, 203); инкрементальная синхронизация и
дедупликация по owner_id+post_id.

Токен: сервисный ключ доступа приложения VK (достаточно для открытых стен),
либо ключ сообщества/пользователя.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import SecretStr

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

log = logging.getLogger(__name__)

GROUP_URL_RE = re.compile(r"(?:https?://)?(?:m\.)?vk\.(?:com|ru)/([\w.]+)", re.I)
VK_LINK_RE = re.compile(r"\[(?:club|public|id)\d+\|([^\]]+)\]")  # [club123|Название] → Название


class VKError(RuntimeError):
    def __init__(self, code: int, message: str, method: str = ""):
        super().__init__(f"VK API {method}: [{code}] {message}")
        self.code = code
        self.message = message
        self.method = method


class VKAuthError(VKError):
    """Неверный или просроченный токен (5), токен не того типа (28, 1116...)."""


class VKAccessError(VKError):
    """Стена/сообщество закрыто или удалено (15, 18, 19, 30, 203)."""


class VKRateLimitError(VKError):
    """Исчерпан лимит запросов (6 — в секунду, 9 — flood, 29 — суточный лимит метода)."""


AUTH_CODES = {5, 27, 28, 1116, 1117}
ACCESS_CODES = {15, 18, 19, 30, 203, 100}
RETRY_CODES = {6, 9, 10}  # 10 — внутренняя ошибка сервера


def parse_group_ref(ref: str) -> str:
    ref = ref.strip()
    if m := GROUP_URL_RE.match(ref):
        ref = m.group(1)
    for prefix in ("club", "public", "event"):
        if ref.startswith(prefix) and ref[len(prefix):].isdigit():
            return ref[len(prefix):]
    return ref.lstrip("-@")


def _largest_photo(photo: dict[str, Any]) -> str | None:
    sizes = photo.get("sizes") or []
    if not sizes:
        return photo.get("orig_photo", {}).get("url")
    best = max(sizes, key=lambda s: (s.get("width", 0) * s.get("height", 0)))
    return best.get("url")


def _attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for a in items or []:
        t = a.get("type")
        body = a.get(t, {}) if t else {}
        entry: dict[str, Any] = {"type": t}
        if t == "photo":
            entry["url"] = _largest_photo(body)
        elif t == "video":
            entry["title"] = body.get("title")
            entry["url"] = f"https://vk.ru/video{body.get('owner_id')}_{body.get('id')}"
            entry["views"] = body.get("views")
            entry["duration"] = body.get("duration")
        elif t == "link":
            entry["url"] = body.get("url")
            entry["title"] = body.get("title")
        elif t == "doc":
            entry["title"] = body.get("title")
            entry["ext"] = body.get("ext")
        elif t == "poll":
            entry["question"] = body.get("question")
            entry["votes"] = body.get("votes")
        elif t == "clip" or t == "short_video":
            entry["url"] = f"https://vk.ru/clip{body.get('owner_id')}_{body.get('id')}"
            entry["views"] = body.get("views")
        out.append(entry)
    return out


def _vk_text(text: str | None) -> str:
    return clean_text(VK_LINK_RE.sub(r"\1", text or ""))


def post_to_document(post: dict[str, Any], group: dict[str, Any]) -> ParsedDocument | None:
    owner_id = post["owner_id"]
    text = _vk_text(post.get("text"))
    reposted = post.get("copy_history") or []
    repost_text = "\n\n".join(_vk_text(r.get("text")) for r in reposted if r.get("text"))
    attachments = _attachments(post.get("attachments"))
    for r in reposted:
        attachments += [dict(a, from_repost=True) for a in _attachments(r.get("attachments"))]
    full_text = text if not repost_text else (f"{text}\n\n[Репост]\n{repost_text}" if text else f"[Репост]\n{repost_text}")
    if not full_text and not attachments:
        return None
    media_types = sorted({a["type"] for a in attachments if a.get("type")})
    return ParsedDocument(
        source_type=SourceType.VK,
        source_id=str(abs(owner_id)),
        external_id=str(post["id"]),
        url=f"https://vk.ru/wall{owner_id}_{post['id']}",
        title=make_title(full_text, media_types),
        text=full_text,
        published_at=datetime.fromtimestamp(post["date"], tz=timezone.utc),
        edited_at=datetime.fromtimestamp(post["edited"], tz=timezone.utc) if post.get("edited") else None,
        metrics={
            "likes": (post.get("likes") or {}).get("count", 0),
            "comments": (post.get("comments") or {}).get("count", 0),
            "reposts": (post.get("reposts") or {}).get("count", 0),
            "views": (post.get("views") or {}).get("count"),  # может отсутствовать
        },
        metadata={
            "group_name": group.get("name"),
            "group_screen_name": group.get("screen_name"),
            "hashtags": extract_hashtags(full_text),
            "media_types": media_types,
            "attachments": attachments,
            "is_repost": bool(reposted),
            "is_pinned": bool(post.get("is_pinned")),
            "marked_as_ads": bool(post.get("marked_as_ads")),
            "post_type": post.get("post_type"),
        },
    )


class VKParser:
    def __init__(self, access_token: SecretStr | str | None, *, api_version: str = "5.199",
                 api_base: str = "https://api.vk.ru/method", min_interval: float = 0.34,
                 max_retries: int = 5, transport: httpx.AsyncBaseTransport | None = None):
        if not access_token:
            raise VKAuthError(5, "Не задан VK_ACCESS_TOKEN. Создайте приложение на https://id.vk.com/about/business/go "
                                 "(или dev.vk.com) и скопируйте «Сервисный ключ доступа».", "config")
        self._token = access_token.get_secret_value() if isinstance(access_token, SecretStr) else access_token
        self.api_version = api_version
        self.api_base = api_base.rstrip("/")
        self.min_interval = min_interval  # ~3 запроса/с — лимит VK для токенов
        self.max_retries = max_retries
        self._transport = transport
        self._last_call = 0.0

    async def call(self, client: httpx.AsyncClient, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        payload.update(access_token=self._token, v=self.api_version)
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            try:
                r = await client.post(f"{self.api_base}/{method}", data=payload)
                r.raise_for_status()
                data = r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt >= self.max_retries:
                    raise VKError(-1, f"сетевая ошибка: {e}", method) from e
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            if "error" not in data:
                return data["response"]
            code = int(data["error"].get("error_code", -1))
            msg = data["error"].get("error_msg", "")
            if code in RETRY_CODES and attempt < self.max_retries:
                await asyncio.sleep(min(1.0 * 2 ** attempt, 30))
                continue
            if code in AUTH_CODES:
                raise VKAuthError(code, msg, method)
            if code in ACCESS_CODES:
                raise VKAccessError(code, msg, method)
            if code in RETRY_CODES or code == 29:
                raise VKRateLimitError(code, msg, method)
            raise VKError(code, msg, method)
        raise VKRateLimitError(6, "превышено число повторов", method)

    async def get_group(self, client: httpx.AsyncClient, ref: str) -> dict[str, Any]:
        resp = await self.call(
            client, "groups.getById", group_id=parse_group_ref(ref),
            fields="description,members_count,site,activity,status,city,verified,contacts,addresses",
        )
        groups = resp.get("groups", []) if isinstance(resp, dict) else resp  # формат зависит от версии API
        if not groups:
            raise VKAccessError(100, f"Сообщество {ref} не найдено", "groups.getById")
        return groups[0]

    async def sync(self, ref: str, store: JsonlDocumentStore, *, max_posts: int = 1000, refresh_recent: int = 100,
                   index_callback: IndexCallback | None = None) -> SyncResult:
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            group = await self.get_group(client, ref)
            gid = int(group["id"])
            owner_id = -gid
            result = SyncResult(source_type=SourceType.VK, source_id=str(gid))
            state_key = f"vk:{gid}"
            state = store.get_state(state_key)
            last_max_id = int(state.get("last_max_post_id", 0))
            if group.get("is_closed") and group.get("is_closed") != 0:
                result.notes.append("сообщество закрытое — доступны только записи, видимые владельцу токена")

            changed: list[ParsedDocument] = []
            offset, fetched, max_id, total = 0, 0, last_max_id, None
            try:
                while fetched < max_posts:
                    count = min(100, max_posts - fetched)
                    page = await self.call(client, "wall.get", owner_id=owner_id, offset=offset, count=count,
                                           filter="all")
                    total = page.get("count", 0)
                    items = page.get("items", [])
                    if not items:
                        break
                    reached_known = False
                    for post in items:
                        max_id = max(max_id, int(post["id"]))
                        known = last_max_id and post["id"] <= last_max_id and not post.get("is_pinned")
                        # известные посты обновляем только в пределах окна refresh_recent (показатели)
                        if known and fetched >= refresh_recent:
                            reached_known = True
                            break
                        fetched += 1
                        doc = post_to_document(post, group)
                        if doc is None:
                            result.skipped += 1
                            continue
                        status = store.upsert(doc)
                        result.count(status)
                        if status in (UpsertStatus.NEW, UpsertStatus.UPDATED):
                            changed.append(doc)
                    offset += len(items)
                    if reached_known or offset >= total:
                        break
            except VKRateLimitError as e:
                result.errors.append(f"{e} — синхронизация остановлена, продолжится со следующего запуска")

            store.set_state(state_key, {
                "ref": ref, "name": group.get("name"), "screen_name": group.get("screen_name"),
                "members_count": group.get("members_count"), "description": group.get("description"),
                "site": group.get("site"), "activity": group.get("activity"), "status": group.get("status"),
                "wall_total": total, "last_max_post_id": max_id, "last_sync": utcnow().isoformat(),
            })
            result.notes.append(f"сообщество «{group.get('name')}» (id {gid}), подписчиков: "
                                f"{group.get('members_count')}, записей на стене: {total}, обработано: {fetched}")
            store.save()
        if index_callback and changed:
            await index_callback(changed)
        result.finished_at = utcnow()
        return result
