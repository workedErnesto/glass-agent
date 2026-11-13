"""CLI для запуска парсеров источников компании.

Примеры:
    python -m scripts.parse_sources --company salavatsteklo website https://salstek.ru/
    python -m scripts.parse_sources --company salavatsteklo telegram https://t.me/+5LuOqWJkZDU0N2Ri
    python -m scripts.parse_sources --company salavatsteklo vk salavatsteklo

Результаты: data/<company>/documents.jsonl, state.json, profile_*.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

from app.core.config import get_settings
from app.parsers.base import JsonlDocumentStore, SourceType, SyncResult


def _print_result(res: SyncResult, elapsed: float) -> None:
    print(f"\n[{res.source_type.value}] {res.source_id}: новых {res.new}, обновлено {res.updated}, "
          f"обновлены показатели {res.metrics_updated}, без изменений {res.unchanged}, "
          f"пропущено {res.skipped}, дублей {res.duplicates}, ошибок {len(res.errors)} — {elapsed:.1f} с")
    for n in res.notes:
        print("  •", n)
    for e in res.errors[:15]:
        print("  ! ", e)
    if len(res.errors) > 15:
        print(f"  ... и ещё {len(res.errors) - 15} ошибок")


async def run_website(args, store: JsonlDocumentStore) -> SyncResult:
    from app.parsers.website_parser import WebsiteParser, WebsiteParserConfig, build_site_profile

    cfg = WebsiteParserConfig(
        max_pages=args.max_pages, max_depth=args.max_depth, concurrency=args.concurrency,
        allowed_languages=tuple(args.lang) if args.lang else (),
    )
    res = await WebsiteParser(cfg).sync(args.url, store)
    profile = build_site_profile(store.all(SourceType.WEBSITE, res.source_id))
    out = store.directory / f"profile_website_{res.source_id}.json"
    out.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    res.notes.append(f"сводка о бизнесе: {out}")
    return res


async def run_telegram(args, store: JsonlDocumentStore) -> SyncResult:
    from app.parsers.telegram_parser import TelethonChannelParser

    s = get_settings()
    parser = TelethonChannelParser(s.tg_api_id, s.tg_api_hash, s.tg_session_path)
    return await parser.sync(args.channel, store, limit=args.limit, join=args.join,
                             refresh_recent=args.refresh_recent)


async def run_telegram_bot(args, store: JsonlDocumentStore) -> SyncResult:
    from app.parsers.telegram_parser import TelegramBotChannelReader

    s = get_settings()
    return await TelegramBotChannelReader(s.tg_bot_token).sync(store)


async def run_telegram_web(args, store: JsonlDocumentStore) -> SyncResult:
    from app.parsers.telegram_nokey import TelegramWebPreviewParser

    return await TelegramWebPreviewParser().sync(args.channel, store, max_posts=args.max_posts,
                                                 initial_pages=args.initial_pages)


async def run_telegram_export(args, store: JsonlDocumentStore) -> SyncResult:
    from app.parsers.telegram_nokey import TelegramExportImporter

    return await TelegramExportImporter().sync(args.path, store)


async def run_vk(args, store: JsonlDocumentStore) -> SyncResult:
    from app.parsers.vk_parser import VKParser

    s = get_settings()
    parser = VKParser(s.vk_access_token, api_version=s.vk_api_version, api_base=s.vk_api_base)
    res = await parser.sync(args.group, store, max_posts=args.max_posts, refresh_recent=args.refresh_recent)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Парсинг источников компании (сайт, Telegram, VK)")
    ap.add_argument("--company", required=True, help="slug компании — каталог данных data/<slug>/")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("website", help="обход сайта")
    w.add_argument("url")
    w.add_argument("--max-pages", type=int, default=300)
    w.add_argument("--max-depth", type=int, default=4)
    w.add_argument("--concurrency", type=int, default=4)
    w.add_argument("--lang", nargs="*", default=["ru"], help="разрешённые языки страниц (пусто = все)")

    t = sub.add_parser("telegram", help="история канала через Telethon (нужна авторизация аккаунта)")
    t.add_argument("channel", help="@username, https://t.me/name или приватная ссылка https://t.me/+hash")
    t.add_argument("--limit", type=int, default=None, help="максимум сообщений за запуск")
    t.add_argument("--join", action="store_true", help="вступить в канал по приглашению, если ещё не участник")
    t.add_argument("--refresh-recent", type=int, default=50, help="обновить показатели последних N постов")

    sub.add_parser("telegram-bot", help="новые посты собственных каналов через Bot API (getUpdates)")

    tw = sub.add_parser("telegram-web", help="открытый канал через веб-превью t.me/s/ (без ключей)")
    tw.add_argument("channel", help="@username или https://t.me/username")
    tw.add_argument("--max-posts", type=int, default=1000)
    tw.add_argument("--initial-pages", type=int, default=1,
                    help="страниц при первом запуске (1 = текущая страница, больше = догрузить историю)")

    te = sub.add_parser("telegram-export", help="закрытый канал: импорт экспорта Telegram Desktop (result.json)")
    te.add_argument("path", help="путь к result.json или к папке экспорта")

    v = sub.add_parser("vk", help="посты сообщества через VK API")
    v.add_argument("group", help="короткое имя, id или ссылка https://vk.ru/name")
    v.add_argument("--max-posts", type=int, default=1000)
    v.add_argument("--refresh-recent", type=int, default=100)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    store = JsonlDocumentStore(settings.data_dir / args.company)
    runner = {"website": run_website, "telegram": run_telegram, "telegram-bot": run_telegram_bot,
              "telegram-web": run_telegram_web, "telegram-export": run_telegram_export, "vk": run_vk}[args.cmd]
    t0 = time.monotonic()
    try:
        res = asyncio.run(runner(args, store))
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА [{args.cmd}]: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    _print_result(res, time.monotonic() - t0)
    print(f"  всего документов компании в хранилище: {len(store)} → {store.docs_path}")
    return 0 if not res.errors else 1


if __name__ == "__main__":
    sys.exit(main())
