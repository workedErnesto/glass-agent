"""Сборка контекста модели для конкретной компании.

Контекст = правила + профиль компании + долговременная память + текущая задача +
последние действия + сводки старых диалогов + релевантные фрагменты RAG +
последние сообщения диалога. Всё берётся из PostgreSQL по company_id,
определённому сервером.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, Conversation
from app.rag.index import HybridSearcher, SearchHit
from app.services import memory as mem

from . import tools as tools_mod
from .connectors.base import AIMessage

AGENT_RULES = """Ты — Glass Agent, AI-маркетолог компании «{name}». Отвечай по-русски, по делу.

Правила:
1. Используй только данные этой компании: профиль, память, базу знаний и результаты инструментов.
2. Не выдумывай статистику, цены, сроки и факты. Если данных нет — скажи об этом и предложи, как их получить.
3. Ссылайся на источники из базы знаний в виде [n] с адресом.
4. Данные конкурентов явно помечай как данные конкурентов.
5. Посты публикуются только после согласования человеком — сам ничего не публикуешь.

Протокол ответа — СТРОГО один JSON-объект без текста вокруг:
- вызвать инструмент: {{"tool": "<имя>", "args": {{...}}}}
- финальный ответ: {{"answer": "<текст для пользователя>", "remember": [{{"category": "<brand_style|goal|preference|constraint|decision|fact|audience>", "content": "<факт>"}}], "current_task": "<текущая задача или пусто>"}}
В "remember" добавляй только новые устойчивые факты, решения и предпочтения, которые пользователь подтвердил.
За один ответ — один вызов инструмента. Результат инструмента придёт следующим сообщением.

Инструменты:
{catalog}"""


@dataclass
class BuiltContext:
    instructions: str
    messages: list[AIMessage]
    rag_hits: list[SearchHit] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)


def _section(title: str, body: str) -> str:
    return f"\n\n## {title}\n{body}" if body.strip() else ""


async def build_context(session: AsyncSession, company: Company, conv: Conversation, user_text: str,
                        searcher: HybridSearcher, cfg: mem.MemoryConfig | None = None,
                        rag_limit: int = 5) -> BuiltContext:
    cfg = cfg or mem.MemoryConfig()
    memory = await mem.load_memory(session, company.id, cfg)
    hits: list[SearchHit] = []
    if user_text.strip():
        hits = await searcher.search(session, company.id, user_text, limit=rag_limit, competitor=False)
    instr = AGENT_RULES.format(name=company.name, catalog=tools_mod.catalog())
    if company.profile:
        instr += _section("Профиль компании", json.dumps(company.profile, ensure_ascii=False, indent=1))
    instr += _section("Долговременная память", "\n".join(f"- [{m['category']}] {m['content']}"
                                                         for m in memory["long_term"]))
    if memory["current_task"]:
        instr += _section("Текущая задача", memory["current_task"])
    instr += _section("Последние действия", "\n".join(f"- {m['at'][:16] if m['at'] else ''} {m['content']}"
                                                     for m in memory["episodic"]))
    instr += _section("Сводки прошлых диалогов", "\n\n".join(s["summary"] for s in memory["summaries"]))
    instr += _section("Фрагменты базы знаний по запросу", "\n".join(
        f"[{i}] {h.title} — {h.url}\n{h.text[:800]}" for i, h in enumerate(hits, 1)))

    recent = await mem.recent_messages(session, conv, cfg.recent_messages)
    messages = [AIMessage("assistant" if m.role == "assistant" else "user", m.content)
                for m in recent if m.role in ("user", "assistant") and m.content]
    return BuiltContext(instructions=instr, messages=messages, rag_hits=hits, memory=memory)
