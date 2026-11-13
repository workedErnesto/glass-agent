"""Оркестратор чата компании: один постоянный диалог на компанию, история и память в PostgreSQL.

Порядок обработки запроса (§4 ТЗ):
1. компания — из авторизованного контекста (передаётся сервером);
2. её conversation; 3. последние сообщения; 4. долговременная память;
5. RAG; 6. контекст модели; 7. вызов модели через разрешённое подключение;
8. сохранение ответа; 9. обновление памяти (+ сводки старых сообщений).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Company
from app.rag.index import HybridSearcher
from app.services import memory as mem

from .agent import Agent, AgentResult
from .connectors.base import AIConnector, AIError
from .context_builder import build_context
from .tools import Budget, ToolContext

log = logging.getLogger(__name__)


@dataclass
class ChatReply:
    message_id: int
    user_message_id: int
    answer: str
    provider: str
    model: str
    is_mock: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    remembered: list[str] = field(default_factory=list)
    created_post_ids: list[str] = field(default_factory=list)
    llm_calls: int = 0
    stopped_reason: str | None = None
    summary_created: bool = False


class Orchestrator:
    def __init__(self, connector: AIConnector, searcher: HybridSearcher, *, memory_cfg: mem.MemoryConfig | None = None,
                 max_steps: int = 6, max_llm_calls: int = 8, max_seconds: float = 240, notifier: Any = None):
        self.connector = connector
        self.notifier = notifier
        self.searcher = searcher
        self.memory_cfg = memory_cfg or mem.MemoryConfig()
        self.max_steps = max_steps
        self.max_llm_calls = max_llm_calls
        self.max_seconds = max_seconds

    async def chat(self, session: AsyncSession, company_id: uuid.UUID, text: str, *,
                   user_id: int | None = None, meta: dict[str, Any] | None = None) -> ChatReply:
        text = (text or "").strip()
        if not text:
            raise ValueError("Пустое сообщение")
        company = await session.get(Company, company_id)
        if company is None:
            raise LookupError("Компания не найдена")
        conv = await mem.get_conversation(session, company_id)
        user_msg = await mem.add_message(session, conv, "user", text, user_id=user_id, meta=meta)
        user_msg_id = user_msg.id
        await session.commit()  # сообщение пользователя сохраняется даже если модель недоступна

        ctx = ToolContext(session=session, company_id=company_id, connector=self.connector, searcher=self.searcher,
                          user_id=user_id, budget=Budget(self.max_llm_calls, self.max_seconds),
                          notifier=self.notifier)
        try:
            built = await build_context(session, company, conv, text, self.searcher, self.memory_cfg)
            result: AgentResult = await Agent(self.connector, max_steps=self.max_steps).run(
                ctx, built.instructions, built.messages)
        except AIError:
            await session.rollback()
            raise

        citations = [{"n": i, "title": h.title, "url": h.url, "source_type": h.source_type}
                     for i, h in enumerate(built.rag_hits, 1)]
        steps = [{"tool": s.tool, "args": s.args, "ok": s.ok} for s in result.steps]
        reply_msg = await mem.add_message(
            session, conv, "assistant", result.answer, provider=result.provider, model=result.model,
            is_mock=result.is_mock,
            meta={"steps": steps, "citations": citations, "llm_calls": result.llm_calls,
                  "stopped_reason": result.stopped_reason, "created_post_ids": ctx.created_post_ids})

        remembered = []
        for item in result.remember[:5]:
            m = await mem.remember(session, company_id, str(item.get("content", "")), kind="long_term",
                                   category=str(item.get("category", "fact")), source_message_id=reply_msg.id,
                                   meta={"is_mock": result.is_mock} if result.is_mock else None)
            if m:
                remembered.append(m.content)
        if result.current_task is not None or result.answer:
            await mem.set_current_task(session, conv, result.current_task)
        session.add(AuditLog(company_id=company_id, user_id=user_id, action="chat.reply",
                             details={"message_id": reply_msg.id, "steps": [s["tool"] for s in steps],
                                      "provider": result.provider, "is_mock": result.is_mock}))
        await session.commit()
        for fn in ctx.after_commit:  # уведомления о согласовании и т.п. — только после фиксации
            try:
                await fn(session)
            except Exception:  # noqa: BLE001
                log.exception("Действие после ответа не выполнено")

        summary_created = False
        if ctx.budget.remaining_calls > 0:
            try:
                summary_created = await mem.summarize_if_needed(session, conv, ctx.metered,
                                                                self.memory_cfg) is not None
                await session.commit()
            except Exception:  # noqa: BLE001 — сводка не критична для ответа (в т.ч. исчерпан лимит)
                log.exception("Не удалось создать сводку")
                await session.rollback()

        return ChatReply(message_id=reply_msg.id, user_message_id=user_msg_id, answer=result.answer,
                         provider=result.provider, model=result.model, is_mock=result.is_mock, steps=steps,
                         citations=citations, remembered=remembered, created_post_ids=ctx.created_post_ids,
                         llm_calls=result.llm_calls, stopped_reason=result.stopped_reason,
                         summary_created=summary_created)
