"""Публикация согласованных постов через Telegram Bot API, уведомления и задания по расписанию.

Защита от дублей: пост атомарно переводится scheduled → publishing (UPDATE … WHERE
status='scheduled'), поэтому параллельные воркеры не отправят его дважды; после
отправки сохраняется message_id. Перед отправкой повторно сверяется approved_hash.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models import AuditLog, Company, CompanyMember, Post, User

from . import memory as mem
from . import posts as P

log = logging.getLogger(__name__)
TG_API = "https://api.telegram.org"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class PublishError(RuntimeError):
    def __init__(self, message: str, *, retry_after: float | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramBotAPI:
    def __init__(self, token: SecretStr | str | None, *, transport: httpx.AsyncBaseTransport | None = None,
                 api_base: str = TG_API):
        raw = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not raw:
            raise PublishError("TG_BOT_TOKEN не задан", permanent=True)
        self._token = raw
        self._transport = transport
        self.api_base = api_base

    async def call(self, method: str, data: dict[str, Any] | None = None,
                   files: dict[str, Any] | None = None, timeout: float = 30) -> Any:
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as c:
            try:
                r = await c.post(f"{self.api_base}/bot{self._token}/{method}", data=data if files else None,
                                 json=None if files else data, files=files)
            except httpx.TransportError as e:
                raise PublishError(f"Сеть: {type(e).__name__}") from e  # токен в сообщение не попадает
        try:
            body = r.json()
        except ValueError:
            raise PublishError(f"Telegram: HTTP {r.status_code}")
        if not body.get("ok"):
            params = body.get("parameters") or {}
            code = body.get("error_code") or r.status_code
            raise PublishError(f"Telegram {code}: {body.get('description')}", retry_after=params.get("retry_after"),
                               permanent=code in (400, 401, 403, 404))
        return body["result"]


class Publisher(Protocol):
    async def publish(self, post: Post, chat_id: str) -> dict[str, Any]: ...


class TelegramPublisher:
    def __init__(self, api: TelegramBotAPI):
        self.api = api

    async def publish(self, post: Post, chat_id: str) -> dict[str, Any]:
        text = post.text.strip()
        if post.image_path and Path(post.image_path).is_file():
            if len(text) <= CAPTION_LIMIT:
                with open(post.image_path, "rb") as f:
                    res = await self.api.call("sendPhoto", {"chat_id": chat_id, "caption": text},
                                              files={"photo": (Path(post.image_path).name, f.read(), "image/png")})
                return _result(res)
            with open(post.image_path, "rb") as f:  # длинный текст: фото, затем текст отдельным сообщением
                await self.api.call("sendPhoto", {"chat_id": chat_id},
                                    files={"photo": (Path(post.image_path).name, f.read(), "image/png")})
        if len(text) > TEXT_LIMIT:
            raise PublishError(f"Текст длиннее {TEXT_LIMIT} символов", permanent=True)
        return _result(await self.api.call("sendMessage", {"chat_id": chat_id, "text": text}))


def _result(res: dict[str, Any]) -> dict[str, Any]:
    chat = res.get("chat") or {}
    url = f"https://t.me/{chat['username']}/{res['message_id']}" if chat.get("username") else None
    return {"message_id": str(res["message_id"]), "url": url, "chat_id": chat.get("id")}


class Notifier(Protocol):
    async def notify(self, session: AsyncSession, company_id: uuid.UUID, text: str) -> None: ...


class TelegramNotifier:
    """Уведомляет владельцев и согласующих компании в личные сообщения бота."""

    def __init__(self, api: TelegramBotAPI):
        self.api = api

    async def notify(self, session: AsyncSession, company_id: uuid.UUID, text: str) -> None:
        ids = (await session.scalars(select(User.telegram_id).join(CompanyMember, CompanyMember.user_id == User.id)
                                     .where(CompanyMember.company_id == company_id,
                                            CompanyMember.role.in_(["owner", "approver"]),
                                            User.telegram_id.is_not(None)))).all()
        for tid in ids:
            try:
                await self.api.call("sendMessage", {"chat_id": tid, "text": text})
            except PublishError as e:
                log.warning("Уведомление %s не доставлено: %s", tid, e)

    async def notify_approval(self, session: AsyncSession, post: Post) -> None:
        """Пост на согласовании → согласующим сообщение с кнопками бота."""
        ids = (await session.scalars(select(User.telegram_id).join(CompanyMember, CompanyMember.user_id == User.id)
                                     .where(CompanyMember.company_id == post.company_id,
                                            CompanyMember.role.in_(["owner", "approver"]),
                                            User.telegram_id.is_not(None)))).all()
        text, kb = approval_message(post)
        for tid in ids:
            try:
                if post.image_path and Path(post.image_path).is_file() and len(text) <= CAPTION_LIMIT:
                    with open(post.image_path, "rb") as f:
                        import json as _json
                        await self.api.call("sendPhoto", {"chat_id": tid, "caption": text,
                                                          "reply_markup": _json.dumps(kb)},
                                            files={"photo": (Path(post.image_path).name, f.read(), "image/png")})
                else:
                    await self.api.call("sendMessage", {"chat_id": tid, "text": text, "reply_markup": kb})
            except PublishError as e:
                log.warning("Уведомление %s не доставлено: %s", tid, e)

    async def notify_admins(self, text: str) -> None:
        for tid in get_settings().admin_ids:
            try:
                await self.api.call("sendMessage", {"chat_id": tid, "text": text[:4000]})
            except PublishError as e:
                log.warning("Уведомление администратору %s не доставлено: %s", tid, e)


def approval_message(post: Post) -> tuple[str, dict[str, Any]]:
    """Сообщение на согласование: материал, площадка, предполагаемое время (п. 3.5 ТЗ) и кнопки."""
    platform = {"telegram": "Telegram", "vk": "ВКонтакте", "max": "MAX"}.get(post.platform, post.platform)
    when = post.proposed_at.astimezone().strftime("%d.%m.%Y %H:%M") if post.proposed_at else "сразу после согласования"
    checks = (post.meta or {}).get("checks") or {}
    warn = "".join(f"\n⚠️ {w}" for w in checks.get("warnings", [])[:3])
    text = (f"📝 На согласование · {platform} · {when}\nТема: {post.topic or '—'} · ревизия {post.revision}"
            f"{' · с изображением' if post.image_path else ''}{warn}\n\n{post.text}")
    if len(text) > 3900:
        text = text[:3900] + "…"
    kb = {"inline_keyboard": [[{"text": "✅ Согласовать", "callback_data": f"ap:{post.id}"},
                               {"text": "🚀 Согласовать и опубликовать", "callback_data": f"pn:{post.id}"}],
                              [{"text": "✏️ На доработку", "callback_data": f"rv:{post.id}"},
                               {"text": "❌ Отклонить", "callback_data": f"rj:{post.id}"}]]}
    return text, kb


class LogNotifier:
    def __init__(self):
        self.sent: list[tuple[uuid.UUID, str]] = []
        self.admin: list[str] = []
        self.approvals: list[str] = []

    async def notify(self, session, company_id, text):
        self.sent.append((company_id, text))
        log.info("[%s] %s", company_id, text)

    async def notify_admins(self, text):
        self.admin.append(text)
        log.warning("[admin] %s", text)

    async def notify_approval(self, session, post):
        text, _ = approval_message(post)
        self.approvals.append(text)
        log.info("[approval] %s", text[:200])


def as_hub(publisher: Any):
    """Один публикатор (для Telegram) или PublisherHub для всех площадок."""
    from .platforms import PublisherHub

    return publisher if isinstance(publisher, PublisherHub) else PublisherHub(overrides={"telegram": publisher})


async def publish_one(sm: async_sessionmaker[AsyncSession], post_id: uuid.UUID, publisher: Any,
                      notifier: Notifier, *, max_attempts: int = 3) -> str:
    """Публикует один запланированный пост. Возвращает итоговый статус."""
    async with sm() as s, s.begin():
        if not await P.claim_for_publishing(s, post_id):
            return "skipped"  # уже публикуется/опубликован другим воркером или снят с расписания
    async with sm() as s:
        post = await s.get(Post, post_id)
        company = await s.get(Company, post.company_id)
        hub = as_hub(publisher)
        error: PublishError | None = None
        res: dict[str, Any] | None = None
        chat_id = None
        if not P.is_approved_content(post):
            error = PublishError("Контент изменён после согласования — нужна повторная проверка", permanent=True)
        else:
            try:
                pub, chat_id = await hub.resolve(s, post, company)
                res = await pub.publish(post, chat_id)
            except PublishError as e:
                error = e
        now = datetime.now(timezone.utc)
        if res is not None:
            post.status, post.published_at = P.PUBLISHED, now
            post.external_message_id, post.external_url = res["message_id"], res.get("url")
            # VK: сохраняем числовой owner_id стены (нужен для сбора комментариев), иначе — указанный канал
            post.target_chat_id = res.get("chat_id") if post.platform == "vk" and res.get("chat_id") else chat_id
            post.last_error = None
            s.add(AuditLog(company_id=post.company_id, action="post.publish",
                           details={"post_id": str(post.id), "message_id": res["message_id"], "chat": chat_id}))
            await mem.remember(s, post.company_id, f"Пост {post.id} опубликован в {chat_id}"
                               f" (сообщение {res['message_id']})", kind="episodic", category="post_published",
                               meta={"post_id": str(post.id), "url": res.get("url")})
            msg = (f"✅ Опубликован пост «{post.topic or post.text[:50]}» ({post.platform})"
                   + (f": {res['url']}" if res.get("url") else ""))
        else:
            post.last_error = str(error)
            if not P.is_approved_content(post):
                post.status = P.DRAFT
                post.approved_hash = None
            elif error.permanent or post.publish_attempts >= max_attempts:
                post.status = P.FAILED
            else:
                post.status = P.SCHEDULED  # повтор с паузой
                delay = error.retry_after or 60 * 2 ** (post.publish_attempts - 1)
                post.scheduled_at = now + timedelta(seconds=delay)
            s.add(AuditLog(company_id=post.company_id, action="post.publish_error",
                           details={"post_id": str(post.id), "error": str(error), "status": post.status}))
            if post.status == P.FAILED:
                await mem.remember(s, post.company_id, f"Публикация поста {post.id} не удалась: {error}",
                                   kind="episodic", category="post_failed", meta={"post_id": str(post.id)})
            msg = (f"⚠️ Пост «{post.topic or post.text[:50]}» не опубликован: {error}. Статус: {post.status}")
        status = post.status
        await s.commit()
        try:
            await notifier.notify(s, post.company_id, msg)
            if status == P.FAILED and hasattr(notifier, "notify_admins"):
                await notifier.notify_admins(f"[{company.name}] {msg}")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка уведомления")
        return status


async def run_due(sm: async_sessionmaker[AsyncSession], publisher: Any, notifier: Notifier,
                  *, now: datetime | None = None, limit: int = 20) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    async with sm() as s:
        ids = (await s.scalars(select(Post.id).where(Post.status == P.SCHEDULED, Post.scheduled_at <= now)
                               .order_by(Post.scheduled_at).limit(limit))).all()
    out = {}
    for pid in ids:
        try:
            out[str(pid)] = await publish_one(sm, pid, publisher, notifier)
        except Exception:  # noqa: BLE001 — один пост не должен останавливать остальные
            log.exception("Ошибка публикации %s", pid)
            out[str(pid)] = "error"
    return out


async def recover_stuck(sm: async_sessionmaker[AsyncSession], *, older_than: timedelta = timedelta(minutes=10)) -> int:
    """Посты, застрявшие в publishing (процесс упал), без message_id → failed для ручной проверки.

    Автоповтор не делаем: сообщение могло уйти в Telegram до падения, повтор дал бы дубль."""
    cutoff = datetime.now(timezone.utc) - older_than
    async with sm() as s, s.begin():
        rows = (await s.scalars(select(Post).where(Post.status == P.PUBLISHING, Post.updated_at < cutoff))).all()
        for p in rows:
            p.status = P.FAILED
            p.last_error = "Публикация прервана: проверьте канал вручную и запланируйте снова при необходимости"
        return len(rows)


async def scheduler_loop(sm: async_sessionmaker[AsyncSession], publisher: Any, notifier: Notifier,
                         *, interval: float = 30, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    log.info("Планировщик публикаций запущен, интервал %s с", interval)
    while not stop.is_set():
        try:
            await recover_stuck(sm)
            done = await run_due(sm, publisher, notifier)
            if done:
                log.info("Публикации: %s", done)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка цикла планировщика")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            pass
