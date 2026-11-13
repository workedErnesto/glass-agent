"""Запуск Telegram-бота Glass Agent.

    python -m scripts.bot
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.ai.factory import get_ai_connector
from app.ai.orchestrator import Orchestrator
from app.bot.bot import GlassBot
from app.core.config import get_settings
from app.db.session import get_sessionmaker, init_db
from app.rag.embeddings import get_embedder
from app.rag.index import HybridSearcher
from app.services.publisher import TelegramBotAPI, TelegramNotifier
from app.services.tasks import TaskDeps


async def main_async() -> int:
    s = get_settings()
    await init_db()
    api = TelegramBotAPI(s.tg_bot_token)
    notifier = TelegramNotifier(api)
    embedder = get_embedder()
    orch = Orchestrator(get_ai_connector(), HybridSearcher(embedder), max_steps=s.agent_max_steps,
                        max_llm_calls=s.agent_max_llm_calls, max_seconds=s.agent_max_seconds, notifier=notifier)
    me = await api.call("getMe")
    logging.getLogger(__name__).info("Бот @%s", me.get("username"))
    await GlassBot(api, get_sessionmaker(), orch, task_deps=TaskDeps(notifier=notifier, embedder=embedder,
                                                                      orchestrator=orch)).run()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # в URL запросов есть токен бота
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
