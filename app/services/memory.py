"""Память компании: short-term, long-term, episodic и сводки диалогов.

Всё хранится в PostgreSQL, поэтому история и память переживают перезапуск.
Оригинальные сообщения при создании сводок не удаляются.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.connectors.base import AIConnector, AIError, AIMessage
from app.db.models import Company, Conversation, ConversationSummary, Memory, Message

log = logging.getLogger(__name__)

LONG_TERM_CATEGORIES = {"brand_style", "goal", "preference", "constraint", "decision", "fact", "audience"}
EPISODIC_CATEGORIES = {"post_created", "post_updated", "post_approved", "post_rejected", "post_scheduled",
                       "post_published", "post_failed", "strategy", "content_plan", "analysis", "action"}


@dataclass
class MemoryConfig:
    recent_messages: int = 20  # сколько последних сообщений идёт в контекст целиком
    summarize_batch: int = 20  # сколько старых сообщений сворачивается в одну сводку
    long_term_limit: int = 40
    episodic_limit: int = 15
    summaries_limit: int = 5


async def get_conversation(session: AsyncSession, company_id: uuid.UUID) -> Conversation:
    """Постоянный диалог компании (создаётся при первом обращении, один на компанию)."""
    conv = await session.scalar(select(Conversation).where(Conversation.company_id == company_id))
    if conv is None:
        if await session.get(Company, company_id) is None:
            raise LookupError("Компания не найдена")
        conv = Conversation(company_id=company_id, state={})
        session.add(conv)
        await session.flush()
    return conv


async def add_message(session: AsyncSession, conv: Conversation, role: str, content: str, *,
                      user_id: int | None = None, provider: str | None = None, model: str | None = None,
                      is_mock: bool = False, meta: dict[str, Any] | None = None) -> Message:
    msg = Message(conversation_id=conv.id, company_id=conv.company_id, role=role, content=content,
                  author_user_id=user_id, provider=provider, model=model, is_mock=is_mock, meta=meta or {})
    session.add(msg)
    await session.flush()
    return msg


async def recent_messages(session: AsyncSession, conv: Conversation, limit: int) -> list[Message]:
    rows = (await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                  .order_by(Message.id.desc()).limit(limit))).all()
    return list(reversed(rows))


async def history(session: AsyncSession, company_id: uuid.UUID, *, limit: int = 50,
                  before_id: int | None = None) -> list[Message]:
    q = select(Message).where(Message.company_id == company_id)
    if before_id:
        q = q.where(Message.id < before_id)
    rows = (await session.scalars(q.order_by(Message.id.desc()).limit(limit))).all()
    return list(reversed(rows))


async def remember(session: AsyncSession, company_id: uuid.UUID, content: str, *, kind: str = "long_term",
                   category: str = "fact", importance: int = 3, source_message_id: int | None = None,
                   meta: dict[str, Any] | None = None) -> Memory | None:
    content = (content or "").strip()
    if not content:
        return None
    if kind == "long_term":
        category = category if category in LONG_TERM_CATEGORIES else "fact"
        # не дублируем один и тот же факт
        exists = await session.scalar(select(Memory.id).where(
            Memory.company_id == company_id, Memory.kind == kind, Memory.is_active.is_(True),
            func.lower(Memory.content) == content.lower()))
        if exists:
            return None
    elif kind == "episodic":
        category = category if category in EPISODIC_CATEGORIES else "action"
    else:
        raise ValueError(f"Неизвестный тип памяти: {kind}")
    m = Memory(company_id=company_id, kind=kind, category=category, content=content[:4000],
               importance=max(1, min(5, int(importance))), source_message_id=source_message_id, meta=meta or {})
    session.add(m)
    await session.flush()
    return m


async def forget(session: AsyncSession, company_id: uuid.UUID, memory_id: int) -> bool:
    m = await session.scalar(select(Memory).where(Memory.id == memory_id, Memory.company_id == company_id))
    if m is None:
        return False
    m.is_active = False
    return True


async def load_memory(session: AsyncSession, company_id: uuid.UUID, cfg: MemoryConfig | None = None) -> dict[str, Any]:
    cfg = cfg or MemoryConfig()
    long_term = (await session.scalars(select(Memory).where(
        Memory.company_id == company_id, Memory.kind == "long_term", Memory.is_active.is_(True))
        .order_by(Memory.importance.desc(), Memory.id.desc()).limit(cfg.long_term_limit))).all()
    episodic = (await session.scalars(select(Memory).where(
        Memory.company_id == company_id, Memory.kind == "episodic", Memory.is_active.is_(True))
        .order_by(Memory.id.desc()).limit(cfg.episodic_limit))).all()
    summaries = (await session.scalars(select(ConversationSummary).where(
        ConversationSummary.company_id == company_id)
        .order_by(ConversationSummary.to_message_id.desc()).limit(cfg.summaries_limit))).all()
    conv = await session.scalar(select(Conversation).where(Conversation.company_id == company_id))
    return {
        "current_task": (conv.state or {}).get("current_task") if conv else None,
        "long_term": [{"id": m.id, "category": m.category, "content": m.content} for m in long_term],
        "episodic": [{"id": m.id, "category": m.category, "content": m.content,
                      "at": m.created_at.isoformat() if m.created_at else None} for m in reversed(episodic)],
        "summaries": [{"from": s.from_message_id, "to": s.to_message_id, "summary": s.summary}
                      for s in reversed(summaries)],
    }


async def set_current_task(session: AsyncSession, conv: Conversation, task: str | None) -> None:
    state = dict(conv.state or {})
    if task:
        state["current_task"] = task[:500]
    else:
        state.pop("current_task", None)
    conv.state = state


SUMMARY_INSTRUCTIONS = (
    "Сожми фрагмент диалога маркетолога с AI в краткую сводку на русском (до 12 пунктов). "
    "Сохрани принятые решения, договорённости, факты о компании, задачи и их статус. "
    "Не добавляй ничего, чего нет в диалоге."
)


async def summarize_if_needed(session: AsyncSession, conv: Conversation, connector: AIConnector,
                              cfg: MemoryConfig | None = None) -> ConversationSummary | None:
    """Сворачивает старые сообщения, не попадающие в окно последних, в сводку. Оригиналы остаются в БД.

    Если модель недоступна — сводка не создаётся (никаких выдуманных сводок), повтор при следующем запросе."""
    cfg = cfg or MemoryConfig()
    last_to = await session.scalar(select(func.max(ConversationSummary.to_message_id))
                                   .where(ConversationSummary.conversation_id == conv.id)) or 0
    recent = await recent_messages(session, conv, cfg.recent_messages)
    if not recent:
        return None
    window_start = recent[0].id
    old = (await session.scalars(select(Message).where(
        Message.conversation_id == conv.id, Message.id > last_to, Message.id < window_start)
        .order_by(Message.id).limit(cfg.summarize_batch))).all()
    if len(old) < cfg.summarize_batch:
        return None
    transcript = "\n".join(f"{m.role}: {m.content[:1500]}" for m in old)
    try:
        res = await connector.complete([AIMessage("user", transcript)], instructions=SUMMARY_INSTRUCTIONS)
    except AIError as e:
        log.warning("Сводка не создана: %s", e)
        return None
    s = ConversationSummary(conversation_id=conv.id, company_id=conv.company_id, from_message_id=old[0].id,
                            to_message_id=old[-1].id,
                            summary=("[ТЕСТОВАЯ МОДЕЛЬ] " if res.is_mock else "") + res.text.strip())
    session.add(s)
    await session.flush()
    return s
