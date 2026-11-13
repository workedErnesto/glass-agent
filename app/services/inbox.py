"""Комментарии и обращения из соцсетей: сбор, черновики ответов, отправка (п. 3.4 ТЗ).

Сбор: VK — wall.getComments под недавними постами сообщества компании (задача collect_comments);
Telegram — бот принимает сообщения в группе обсуждений канала (settings.channels.telegram_discussion).
Ответы: черновик готовит AI (или человек), отправляет человек с ролью approver.
Автоматическая отправка — только если у компании включено settings.permissions.auto_reply
(отдельное разрешение, по умолчанию выключено). MAX: сбор комментариев пока не реализован.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.connectors.base import AIError
from app.core.config import get_settings
from app.db.models import AuditLog, Company, Document, InboxItem, Post

from . import secrets

log = logging.getLogger(__name__)
NEW, DRAFT_READY, REPLIED, IGNORED, ERROR = "new", "draft_ready", "replied", "ignored", "error"

REPLY_RULES = ("Ты отвечаешь от имени компании на комментарий в соцсети. Вежливо, коротко (1–3 предложения), по делу, "
               "на русском. Используй только факты из данных компании; если ответа нет — предложи связаться с менеджером "
               "и не выдумывай цены, сроки и обещания. Верни только текст ответа.")


def permissions(company: Company) -> dict[str, Any]:
    return {"auto_reply": False, **((company.settings or {}).get("permissions") or {})}


def item_dict(i: InboxItem) -> dict[str, Any]:
    return {"id": str(i.id), "platform": i.platform, "chat_ref": i.chat_ref, "external_id": i.external_id,
            "post_ref": i.post_ref, "author_name": i.author_name, "text": i.text, "status": i.status,
            "received_at": i.received_at.isoformat() if i.received_at else None, "reply_text": i.reply_text,
            "reply_meta": i.reply_meta, "replied_at": i.replied_at.isoformat() if i.replied_at else None}


async def upsert_item(session: AsyncSession, company_id: uuid.UUID, *, platform: str, chat_ref: str, external_id: str,
                      text: str, author_id: str | None = None, author_name: str = "", post_ref: str | None = None,
                      received_at: datetime | None = None, meta: dict[str, Any] | None = None) -> bool:
    exists = await session.scalar(select(InboxItem.id).where(
        InboxItem.company_id == company_id, InboxItem.platform == platform, InboxItem.chat_ref == chat_ref,
        InboxItem.external_id == external_id))
    if exists:
        return False
    session.add(InboxItem(company_id=company_id, platform=platform, chat_ref=chat_ref, external_id=external_id,
                          text=text[:4000], author_id=author_id, author_name=author_name[:300], post_ref=post_ref,
                          received_at=received_at, status=NEW, meta=meta or {}, reply_meta={}))
    return True


async def record_telegram_message(session: AsyncSession, msg: dict[str, Any]) -> uuid.UUID | None:
    """Сообщение в группе обсуждений канала → входящие компании, к которой привязана группа."""
    chat_id = str(msg.get("chat", {}).get("id"))
    if not msg.get("text") or (msg.get("from") or {}).get("is_bot") or msg.get("is_automatic_forward"):
        return None  # автопересылка поста канала в обсуждение — не комментарий
    companies = (await session.scalars(select(Company))).all()
    company = next((c for c in companies
                    if str(((c.settings or {}).get("channels") or {}).get("telegram_discussion") or "") == chat_id), None)
    if company is None:
        return None
    frm = msg.get("from") or {}
    reply_to = msg.get("reply_to_message") or {}
    await upsert_item(session, company.id, platform="telegram", chat_ref=chat_id, external_id=str(msg["message_id"]),
                      text=msg["text"], author_id=str(frm.get("id")),
                      author_name=" ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
                      or frm.get("username", ""),
                      post_ref=str(reply_to.get("forward_from_message_id") or reply_to.get("message_id") or "") or None,
                      received_at=datetime.fromtimestamp(msg.get("date", 0), tz=timezone.utc))
    return company.id


async def _vk_token(session: AsyncSession, company_id: uuid.UUID) -> str | None:
    s = get_settings()
    tok = await secrets.get_secret(session, company_id, "vk_token")
    if tok:
        return tok
    for t in (s.vk_publish_token, s.vk_access_token):
        if t:
            return t.get_secret_value()
    return None


async def collect_vk_comments(sm: async_sessionmaker[AsyncSession], company_id: uuid.UUID, *, days: int = 14,
                              transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    from app.parsers.vk_parser import VKError, VKParser

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with sm() as s:
        token = await _vk_token(s, company_id)
        docs = (await s.scalars(select(Document).where(
            Document.company_id == company_id, Document.source_type == "vk", Document.is_competitor.is_(False),
            Document.published_at >= since).limit(50))).all()
        posts = [(int(d.external_key.split(":")[1]), int(d.external_key.split(":")[2])) for d in docs]
        published = (await s.scalars(select(Post).where(Post.company_id == company_id, Post.platform == "vk",
                                                        Post.status == "published", Post.published_at >= since))).all()
        for p in published:
            if p.external_message_id and p.target_chat_id:
                posts.append((abs(int(str(p.target_chat_id).lstrip("-"))), int(p.external_message_id)))
    posts = list(dict.fromkeys(posts))
    if not posts:
        return {"new": 0, "note": "Нет недавних постов VK компании"}
    if not token:
        return {"new": 0, "note": "Нет ключа VK (секрет компании vk_token или VK_ACCESS_TOKEN)"}
    api = VKParser(token, api_version=get_settings().vk_api_version, api_base=get_settings().vk_api_base,
                   transport=transport)
    new, errors = 0, []
    async with httpx.AsyncClient(timeout=30, transport=transport) as client, sm() as s:
        for gid, post_id in posts:
            try:
                res = await api.call(client, "wall.getComments", owner_id=-gid, post_id=post_id, count=100,
                                     sort="desc", extended=1)
            except VKError as e:
                errors.append(str(e))
                continue
            names = {p["id"]: f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                     for p in res.get("profiles") or []}
            names.update({-g["id"]: g.get("name", "") for g in res.get("groups") or []})
            for c in res.get("items") or []:
                if c.get("from_id") == -gid or not c.get("text"):
                    continue  # свои ответы сообщества и пустые не собираем
                new += await upsert_item(s, company_id, platform="vk", chat_ref=str(-gid), external_id=str(c["id"]),
                                         text=c["text"], author_id=str(c.get("from_id")),
                                         author_name=names.get(c.get("from_id"), ""), post_ref=f"{-gid}_{post_id}",
                                         received_at=datetime.fromtimestamp(c.get("date", 0), tz=timezone.utc))
        await s.commit()
    return {"new": new, "posts_checked": len(posts), "errors": errors[:5]}


async def draft_reply(session: AsyncSession, item: InboxItem, connector: Any, searcher: Any) -> InboxItem:
    from app.ai.tools import Budget, ToolContext, _company_brief

    ctx = ToolContext(session=session, company_id=item.company_id, connector=connector, searcher=searcher,
                      budget=Budget(max_llm_calls=1))
    brief = await _company_brief(ctx, item.text)
    res = await ctx.llm(f"{brief}\n\nКомментарий ({item.platform}) от {item.author_name or 'пользователя'}:\n"
                        f"{item.text}", REPLY_RULES)
    item.reply_text = res.text.strip()
    item.reply_meta = {**ctx.ai_meta(), "drafted_at": datetime.now(timezone.utc).isoformat()}
    item.status = DRAFT_READY
    return item


async def send_reply(session: AsyncSession, item: InboxItem, *, user_id: int | None = None,
                     transport: httpx.AsyncBaseTransport | None = None, auto: bool = False,
                     telegram_api: Any = None) -> InboxItem:
    from .publisher import PublishError, TelegramBotAPI

    if item.status == REPLIED:
        raise ValueError("Ответ уже отправлен")
    text = (item.reply_text or "").strip()
    if not text:
        raise ValueError("Нет текста ответа")
    s = get_settings()
    try:
        if item.platform == "telegram":
            api = telegram_api
            if api is None:
                token = await secrets.get_secret(session, item.company_id, "telegram_bot_token") or (
                    s.tg_bot_token.get_secret_value() if s.tg_bot_token else None)
                api = TelegramBotAPI(token, transport=transport)
            res = await api.call("sendMessage", {
                "chat_id": item.chat_ref, "text": text, "reply_parameters": {"message_id": int(item.external_id)}})
            item.reply_external_id = str(res.get("message_id"))
        elif item.platform == "vk":
            from app.parsers.vk_parser import VKError, VKParser

            token = await secrets.get_secret(session, item.company_id, "vk_token") or (
                s.vk_publish_token.get_secret_value() if s.vk_publish_token else None)
            if not token:
                raise PublishError("Нет ключа VK с правом wall (секрет компании vk_token)", permanent=True)
            owner, post_id = item.post_ref.split("_")
            api = VKParser(token, api_version=s.vk_api_version, api_base=s.vk_api_base, transport=transport)
            async with httpx.AsyncClient(timeout=30, transport=transport) as client:
                try:
                    res = await api.call(client, "wall.createComment", owner_id=int(owner), post_id=int(post_id),
                                         from_group=abs(int(owner)), message=text,
                                         reply_to_comment=int(item.external_id))
                except VKError as e:
                    raise PublishError(str(e)) from e
            item.reply_external_id = str(res.get("comment_id"))
        else:
            raise PublishError(f"Ответы на {item.platform} пока не поддерживаются", permanent=True)
    except PublishError as e:
        item.status = ERROR
        item.reply_meta = {**(item.reply_meta or {}), "error": str(e)}
        raise
    item.status, item.replied_at = REPLIED, datetime.now(timezone.utc)
    item.reply_meta = {**(item.reply_meta or {}), "auto": auto, "sent_by_user_id": user_id}
    session.add(AuditLog(company_id=item.company_id, user_id=user_id, action="inbox.reply",
                         details={"item_id": str(item.id), "platform": item.platform, "auto": auto}))
    return item


async def auto_reply(sm: async_sessionmaker[AsyncSession], company_id: uuid.UUID, deps: Any, *, limit: int = 10) -> int:
    """Автоответы — только при разрешении settings.permissions.auto_reply. Возвращает число отправленных."""
    orch = getattr(deps, "orchestrator", None)
    async with sm() as s:
        company = await s.get(Company, company_id)
        if not permissions(company)["auto_reply"] or orch is None:
            return 0
        items = (await s.scalars(select(InboxItem).where(InboxItem.company_id == company_id, InboxItem.status == NEW)
                                 .order_by(InboxItem.received_at).limit(limit))).all()
        sent = 0
        for item in items:
            try:
                await draft_reply(s, item, orch.connector, orch.searcher)
                await send_reply(s, item, auto=True)
                sent += 1
            except AIError:
                break  # модель недоступна — остальное позже
            except Exception as e:  # noqa: BLE001
                log.warning("Автоответ %s не отправлен: %s", item.id, e)
            await s.commit()
        return sent
