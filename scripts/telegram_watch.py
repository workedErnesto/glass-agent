"""Сбор постов открытого Telegram-канала через веб-страницу t.me/s/<имя> с периодическим опросом.

Без Bot API, Telethon, api_id/api_hash и входа в аккаунт — только HTTP и разбор HTML.
Настройки в .env:
    TG_WEB_CHANNEL=salstek           # имя канала или ссылка
    TG_WEB_POLL_INTERVAL=60          # секунд между проверками
    TG_WEB_INITIAL_PAGES=1           # первый запуск: текущая страница (~20 постов)
    TG_WEB_COMPANY=salavatsteklo     # компания, в базу знаний которой сохраняются посты

Запуск:
    python -m scripts.telegram_watch            # опрос каждые TG_WEB_POLL_INTERVAL секунд (Ctrl+C — остановка)
    python -m scripts.telegram_watch --once     # один проход
    python -m scripts.telegram_watch --store jsonl   # без PostgreSQL: data/<компания>/documents.jsonl

Результат: PostgreSQL (таблица documents) — смотреть: python -m scripts.manage posts salavatsteklo
Лог: logs/telegram_web.log
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from app.core.config import get_settings
from app.parsers.base import JsonlDocumentStore, SyncResult
from app.parsers.telegram_nokey import TelegramWebPreviewParser
from app.parsers.telegram_parser import TelegramAccessError, parse_channel_ref

log = logging.getLogger("glass.telegram_watch")


def setup_logging(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger("glass")
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]
    root.propagate = False


async def run_once_db(channel: str, company_slug: str, initial_pages: int) -> SyncResult:
    from sqlalchemy import select

    from app.db.models import Company
    from app.db.session import get_sessionmaker, init_db
    from app.rag.embeddings import get_embedder
    from app.services.sources import add_source, create_company, sync_source

    await init_db()
    sm = get_sessionmaker()
    async with sm() as s, s.begin():
        company = await s.scalar(select(Company).where(Company.slug == company_slug))
        if company is None:
            company = await create_company(s, company_slug, slug=company_slug)
        src = await add_source(s, company.id, "telegram_web", channel, options={"initial_pages": initial_pages})
        source_id, company_id = src.id, company.id
    return await sync_source(sm, source_id, get_embedder(), company_id=company_id)


async def run_once_jsonl(channel: str, company_slug: str, initial_pages: int) -> SyncResult:
    store = JsonlDocumentStore(get_settings().data_dir / company_slug)
    return await TelegramWebPreviewParser().sync(channel, store, initial_pages=initial_pages)


async def watch(channel: str, company: str, interval: int, initial_pages: int, store: str, once: bool) -> int:
    runner = run_once_db if store == "db" else run_once_jsonl
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("Старт: канал %s, интервал %s с, хранилище %s, компания %s", channel, interval, store, company)
    while not stop.is_set():
        try:
            res = await runner(channel, company, initial_pages)
            if res.errors:
                log.error("Проход с ошибкой (позиция сохранена, повтор через %s с): %s", interval, res.errors[0])
            else:
                log.info("Проход: новых %d, обновлено %d, показатели %d. %s", res.new, res.updated,
                         res.metrics_updated, " | ".join(res.notes))
        except TelegramAccessError as e:
            log.error("Канал недоступен через веб-страницу, опрос остановлен: %s", e)
            return 2
        except Exception as e:  # noqa: BLE001 — любой сбой одного прохода не останавливает опрос
            log.exception("Проход прерван: %s: %s (позиция сохранена, повтор через %s с)", type(e).__name__, e, interval)
        if once:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("Опрос остановлен")
    return 0


def main() -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description="Сбор постов открытого Telegram-канала через t.me/s/")
    ap.add_argument("--channel", default=s.tg_web_channel, help="переопределить TG_WEB_CHANNEL")
    ap.add_argument("--interval", type=int, default=s.tg_web_poll_interval, help="переопределить TG_WEB_POLL_INTERVAL")
    ap.add_argument("--company", default=s.tg_web_company)
    ap.add_argument("--store", choices=["db", "jsonl"], default="db")
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    a = ap.parse_args()
    setup_logging(s.tg_web_log_path)
    if not a.channel:
        log.error("Не задан канал: укажите TG_WEB_CHANNEL в .env или --channel")
        return 2
    kind, value = parse_channel_ref(a.channel)
    if kind != "username":
        log.error("%s — ссылка-приглашение закрытого канала: веб-страницы t.me/s/ с постами у него нет. "
                  "Укажите открытый канал или используйте импорт экспорта Telegram Desktop.", a.channel)
        return 2
    return asyncio.run(watch(value, a.company, max(10, a.interval), s.tg_web_initial_pages, a.store, a.once))


if __name__ == "__main__":
    sys.exit(main())
