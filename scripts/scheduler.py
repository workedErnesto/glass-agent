"""Планировщик: публикация согласованных постов (Telegram, VK, MAX) и регулярные задачи компаний.

    python -m scripts.scheduler            # постоянно, проверка каждые SCHEDULER_INTERVAL секунд
    python -m scripts.scheduler --once     # один проход
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.ai.factory import get_ai_connector
from app.ai.orchestrator import Orchestrator
from app.core.config import get_settings
from app.db.session import get_sessionmaker, init_db
from app.rag.embeddings import get_embedder
from app.rag.index import HybridSearcher
from app.services.platforms import PublisherHub
from app.services.publisher import LogNotifier, TelegramBotAPI, TelegramNotifier, recover_stuck, run_due
from app.services.tasks import TaskDeps, run_due_tasks, scheduler_loop_all


def build_deps():
    s = get_settings()
    notifier = TelegramNotifier(TelegramBotAPI(s.tg_bot_token)) if s.tg_bot_token else LogNotifier()
    embedder = get_embedder()
    orch = Orchestrator(get_ai_connector(), HybridSearcher(embedder), max_steps=s.agent_max_steps,
                        max_llm_calls=s.agent_max_llm_calls, max_seconds=s.agent_max_seconds, notifier=notifier)
    return PublisherHub(), TaskDeps(notifier=notifier, embedder=embedder, orchestrator=orch)


async def main_async(once: bool) -> int:
    s = get_settings()
    await init_db()
    sm = get_sessionmaker()
    hub, deps = build_deps()
    if once:
        await recover_stuck(sm)
        print("Публикации:", await run_due(sm, hub, deps.notifier) or "нет")
        print("Задачи:", await run_due_tasks(sm, deps) or "нет")
        return 0
    await scheduler_loop_all(sm, hub, deps, interval=s.scheduler_interval)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # в URL запросов есть токен бота
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    try:
        return asyncio.run(main_async(ap.parse_args().once))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
