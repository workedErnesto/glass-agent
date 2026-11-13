"""Площадки публикации: Telegram, ВКонтакте, MAX. Новые площадки добавляются классом с методом publish().

Куда публиковать: post.target_chat_id или settings.channels[<платформа>] компании
(для Telegram также устаревшее settings.telegram_channel).
Токены: секреты компании (vk_token, max_bot_token, telegram_bot_token) или общие из .env.

ВНИМАНИЕ: VK и MAX реализованы по официальной документации и проверены на моках;
на реальных аккаунтах не проверялись (нет ключей) — см. docs/HANDOFF.md.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Company, Post

from .publisher import PublishError, TelegramBotAPI, TelegramPublisher

log = logging.getLogger(__name__)
PLATFORMS = ("telegram", "vk", "max")
TEXT_LIMITS = {"telegram": 4096, "telegram_caption": 1024, "vk": 15895, "max": 4000}


class Publisher(Protocol):
    async def publish(self, post: Post, target: str) -> dict[str, Any]: ...


def _raw(token: SecretStr | str | None) -> str | None:
    return token.get_secret_value() if isinstance(token, SecretStr) else token


def target_for(post: Post, company: Company) -> str | None:
    if post.target_chat_id:
        return post.target_chat_id
    settings = company.settings or {}
    target = (settings.get("channels") or {}).get(post.platform)
    if not target and post.platform == "telegram":
        target = settings.get("telegram_channel")
    return str(target) if target else None


# --------------------------------------------------------------------------- ВКонтакте

class VKPublisher:
    """wall.post от имени сообщества; фото — photos.getWallUploadServer → upload → photos.saveWallPhoto.

    Нужен ключ пользователя-администратора сообщества с правами wall, photos, offline
    (сервисный ключ приложения публиковать не может)."""

    def __init__(self, token: str, *, api_version: str = "5.199", api_base: str = "https://api.vk.ru/method",
                 transport: httpx.AsyncBaseTransport | None = None):
        from app.parsers.vk_parser import VKParser

        self.api = VKParser(token, api_version=api_version, api_base=api_base, transport=transport)
        self._transport = transport

    async def _group_id(self, client: httpx.AsyncClient, target: str) -> int:
        from app.parsers.vk_parser import parse_group_ref

        ref = parse_group_ref(str(target))
        if ref.lstrip("-").isdigit():
            return abs(int(ref))
        return int((await self.api.get_group(client, ref))["id"])

    async def publish(self, post: Post, target: str) -> dict[str, Any]:
        from app.parsers.vk_parser import VKAccessError, VKAuthError, VKError, VKRateLimitError

        text = post.text.strip()
        if len(text) > TEXT_LIMITS["vk"]:
            raise PublishError("Текст длиннее лимита VK", permanent=True)
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                gid = await self._group_id(client, target)
                attachments = None
                if post.image_path and Path(post.image_path).is_file():
                    up = await self.api.call(client, "photos.getWallUploadServer", group_id=gid)
                    with open(post.image_path, "rb") as f:
                        r = await client.post(up["upload_url"], files={"photo": (Path(post.image_path).name, f.read(),
                                                                                 "image/png")})
                    r.raise_for_status()
                    uploaded = r.json()
                    if not uploaded.get("photo") or uploaded.get("photo") == "[]":
                        raise PublishError("VK не принял изображение", permanent=True)
                    saved = await self.api.call(client, "photos.saveWallPhoto", group_id=gid, photo=uploaded["photo"],
                                                server=uploaded["server"], hash=uploaded["hash"])
                    attachments = f"photo{saved[0]['owner_id']}_{saved[0]['id']}"
                res = await self.api.call(client, "wall.post", owner_id=-gid, from_group=1, message=text,
                                          attachments=attachments)
        except VKRateLimitError as e:
            raise PublishError(str(e), retry_after=60) from e
        except (VKAuthError, VKAccessError) as e:
            raise PublishError(str(e), permanent=True) from e
        except VKError as e:
            raise PublishError(str(e), permanent=e.code in (14, 15, 214, 219, 220, 222)) from e
        except httpx.HTTPError as e:
            raise PublishError(f"VK: сеть {type(e).__name__}") from e
        pid = res["post_id"]
        return {"message_id": str(pid), "url": f"https://vk.com/wall-{gid}_{pid}", "chat_id": f"-{gid}"}


# --------------------------------------------------------------------------- MAX

MAX_API = "https://platform-api2.max.ru"


class MaxAPI:
    """MAX Bot API (dev.max.ru/docs-api): токен в заголовке Authorization, лимит 30 rps и 2 сообщения/с на чат."""

    def __init__(self, token: str, *, api_base: str = MAX_API, transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise PublishError("MAX_BOT_TOKEN не задан", permanent=True)
        self._token = token
        self.api_base = api_base.rstrip("/")
        self._transport = transport

    def _client(self, timeout: float = 60) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport, headers={"Authorization": self._token})

    async def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        async with self._client() as c:
            try:
                r = await c.request(method, f"{self.api_base}{path}", params=params, json=json)
            except httpx.TransportError as e:
                raise PublishError(f"MAX: сеть {type(e).__name__}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429:
            raise PublishError("MAX: превышен лимит запросов", retry_after=5)
        if r.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False):
            code = body.get("code") if isinstance(body, dict) else None
            err = PublishError(f"MAX {r.status_code} {code or ''}: {body.get('message', '') if isinstance(body, dict) else ''}",
                               permanent=r.status_code in (400, 401, 403, 404, 405, 415))
            err.code = code  # type: ignore[attr-defined]
            raise err
        return body

    async def upload_image(self, path: Path) -> str:
        up = await self.request("POST", "/uploads", params={"type": "image"})
        async with self._client(120) as c:
            r = await c.post(up["url"], files={"data": (path.name, path.read_bytes(), "image/png")})
        if r.status_code >= 400:
            raise PublishError(f"MAX: загрузка изображения HTTP {r.status_code}")
        token = _find_token(r.json())
        if not token:
            raise PublishError("MAX: в ответе загрузки нет token", permanent=True)
        return token


def _find_token(obj: Any) -> str | None:
    """Ответ загрузки: {"photos": {"<id>": {"token": "..."}}} — берём первый вложенный token."""
    if isinstance(obj, dict):
        if isinstance(obj.get("token"), str):
            return obj["token"]
        for v in obj.values():
            if t := _find_token(v):
                return t
    return None


class MaxPublisher:
    def __init__(self, api: MaxAPI, *, attachment_retries: int = 4, retry_base_s: float = 1.0):
        self.api = api
        self.attachment_retries = attachment_retries
        self.retry_base_s = retry_base_s

    async def publish(self, post: Post, target: str) -> dict[str, Any]:
        text = post.text.strip()
        if len(text) > TEXT_LIMITS["max"]:
            raise PublishError("Текст длиннее 4000 символов (лимит MAX)", permanent=True)
        body: dict[str, Any] = {"text": text}
        if post.image_path and Path(post.image_path).is_file():
            token = await self.api.upload_image(Path(post.image_path))
            body["attachments"] = [{"type": "image", "payload": {"token": token}}]
        for attempt in range(self.attachment_retries + 1):
            try:
                res = await self.api.request("POST", "/messages", params={"chat_id": target}, json=body)
                break
            except PublishError as e:
                # вложение ещё обрабатывается — пауза с ростом интервала (документация MAX)
                if getattr(e, "code", None) == "attachment.not.ready" and attempt < self.attachment_retries:
                    await asyncio.sleep(self.retry_base_s * 2 ** attempt)
                    continue
                raise
        msg = res.get("message") or {}
        mid = (msg.get("body") or {}).get("mid")
        if not mid:
            raise PublishError("MAX: в ответе нет message.body.mid")
        url = msg.get("url") or (msg.get("stat") or {}).get("url")
        return {"message_id": str(mid), "url": url, "chat_id": str(target)}


# --------------------------------------------------------------------------- выбор площадки

class PublisherHub:
    """Подбирает публикатора по post.platform с токеном компании или общим."""

    def __init__(self, *, overrides: dict[str, Publisher] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.overrides = overrides or {}
        self.transport = transport

    async def resolve(self, session: AsyncSession, post: Post, company: Company) -> tuple[Publisher, str]:
        from . import secrets

        if post.platform not in PLATFORMS:
            raise PublishError(f"Площадка {post.platform} не поддерживается", permanent=True)
        target = target_for(post, company)
        if not target:
            raise PublishError(f"Не указан канал {post.platform}: settings.channels.{post.platform} компании",
                               permanent=True)
        if post.platform in self.overrides:
            return self.overrides[post.platform], target
        s = get_settings()
        if post.platform == "telegram":
            token = await secrets.get_secret(session, company.id, "telegram_bot_token") or _raw(s.tg_bot_token)
            return TelegramPublisher(TelegramBotAPI(token, transport=self.transport)), target
        if post.platform == "vk":
            token = await secrets.get_secret(session, company.id, "vk_token") or _raw(s.vk_publish_token)
            if not token:
                raise PublishError("Нет ключа VK для публикации: секрет компании vk_token или VK_PUBLISH_TOKEN",
                                   permanent=True)
            return VKPublisher(token, api_version=s.vk_api_version, api_base=s.vk_api_base,
                               transport=self.transport), target
        token = await secrets.get_secret(session, company.id, "max_bot_token") or _raw(s.max_bot_token)
        return MaxPublisher(MaxAPI(token or "", api_base=s.max_api_base, transport=self.transport)), target
