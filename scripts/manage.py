"""Администрирование: компании, источники, синхронизация, поиск по знаниям.

    python -m scripts.manage init-db
    python -m scripts.manage create-company "Салаватстекло" --slug salavatsteklo
    python -m scripts.manage add-source salavatsteklo website https://salstek.ru/
    python -m scripts.manage sync salavatsteklo            # все источники компании
    python -m scripts.manage search salavatsteklo "энергосберегающее стекло для фасада"
    python -m scripts.manage list
    python -m scripts.manage add-member salavatsteklo 123456789 owner
    python -m scripts.manage settings salavatsteklo channels.telegram=@salstek_test brand.banned_words='["дешёвый"]'
    python -m scripts.manage secret salavatsteklo vk_token        # значение вводится скрыто
    python -m scripts.manage default-tasks salavatsteklo
    python -m scripts.manage tasks salavatsteklo
    python -m scripts.manage run-task salavatsteklo report
    python -m scripts.manage report salavatsteklo --days 30
    python -m scripts.manage export-openapi docs/openapi.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from sqlalchemy import func, select

from app.db.models import Chunk, Company, Source
from app.db.session import get_sessionmaker, init_db
from app.rag.embeddings import get_embedder
from app.rag.index import HybridSearcher
from app.services.sources import add_source, create_company, sync_source


async def _company(session, slug: str) -> Company:
    c = await session.scalar(select(Company).where(Company.slug == slug))
    if c is None:
        raise SystemExit(f"Компания {slug!r} не найдена")
    return c


async def main_async(a) -> int:
    sm = get_sessionmaker()
    if a.cmd == "init-db":
        await init_db()
        print("Схема БД создана")
    elif a.cmd == "create-company":
        await init_db()
        async with sm() as s, s.begin():
            c = await create_company(s, a.name, slug=a.slug)
        print(f"Компания создана: {c.name} slug={c.slug} id={c.id}")
    elif a.cmd == "add-source":
        async with sm() as s, s.begin():
            c = await _company(s, a.slug)
            src = await add_source(s, c.id, a.type, a.ref, is_competitor=a.competitor,
                                   options=json.loads(a.options) if a.options else None)
        print(f"Источник: {src.type} {src.ref} id={src.id} competitor={src.is_competitor}")
    elif a.cmd == "sync":
        async with sm() as s:
            c = await _company(s, a.slug)
            q = select(Source).where(Source.company_id == c.id)
            if a.type:
                q = q.where(Source.type == a.type)
            sources = (await s.scalars(q)).all()
        code = 0
        for src in sources:
            t0 = time.monotonic()
            try:
                r = await sync_source(sm, src.id, get_embedder(), company_id=c.id)
            except Exception as e:  # noqa: BLE001
                print(f"[{src.type}] {src.ref}: ОШИБКА {type(e).__name__}: {e}")
                code = 1
                continue
            print(f"[{src.type}] {src.ref}: новых {r.new}, обновлено {r.updated}, показатели {r.metrics_updated}, "
                  f"без изменений {r.unchanged}, дублей {r.duplicates}, ошибок {len(r.errors)} "
                  f"— {time.monotonic() - t0:.1f} с")
            for n in r.notes:
                print("   •", n)
        return code  # 1, если хотя бы один источник не синхронизировался
    elif a.cmd == "search":
        async with sm() as s, s.begin():
            c = await _company(s, a.slug)
            hits = await HybridSearcher(get_embedder()).search(s, c.id, a.query, limit=a.limit,
                                                                competitor=None if a.all else False)
        for i, h in enumerate(hits, 1):
            snippet = h.text.replace("\n", " ")[:220]
            print(f"{i}. {h.title} [{h.source_type}] rrf={h.score:.4f} sem#{h.semantic_rank} kw#{h.keyword_rank}\n"
                  f"   {h.url}\n   {snippet}…")
    elif a.cmd == "posts":
        from app.db.models import Document
        async with sm() as s:
            c = await _company(s, a.slug)
            q = (select(Document).join(Source, Source.id == Document.source_id)
                 .where(Document.company_id == c.id, Source.type == a.type)
                 .order_by(Document.published_at.desc().nulls_last()).limit(a.limit))
            rows = (await s.scalars(q)).all()
        for d in reversed(rows):  # от старых к новым
            pid = (d.meta or {}).get("post_id") or d.external_key.rsplit(":", 1)[-1]
            text = d.text.replace("\n", " ")[:90] or "[без текста: " + ", ".join((d.meta or {}).get("media_types", [])) + "]"
            date = d.published_at.strftime("%Y-%m-%d %H:%M") if d.published_at else "—"
            print(f"{pid:>6}  {date}  {d.url}\n        {text}")
        print(f"показано {len(rows)}")
    elif a.cmd == "add-member":
        from app.db.models import CompanyMember, User
        async with sm() as s, s.begin():
            c = await _company(s, a.slug)
            u = await s.scalar(select(User).where(User.telegram_id == a.telegram_id))
            if u is None:
                u = User(telegram_id=a.telegram_id, prefs={})
                s.add(u)
                await s.flush()
            m = await s.get(CompanyMember, (c.id, u.id))
            if m is None:
                s.add(CompanyMember(company_id=c.id, user_id=u.id, role=a.role))
            else:
                m.role = a.role
        print(f"{a.telegram_id} → {a.role} в {a.slug}")
    elif a.cmd == "settings":
        async with sm() as s, s.begin():
            c = await _company(s, a.slug)
            settings = json.loads(json.dumps(c.settings or {}))
            for item in a.pairs:
                key, _, raw = item.partition("=")
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = raw
                node = settings
                *path, last = key.split(".")
                for k in path:
                    node = node.setdefault(k, {})
                if raw == "":
                    node.pop(last, None)
                else:
                    node[last] = value
            c.settings = settings
        print(json.dumps(settings, ensure_ascii=False, indent=2))
    elif a.cmd == "secret":
        import getpass

        from app.services import secrets
        value = getpass.getpass(f"{a.name} (ввод скрыт): ")
        async with sm() as s, s.begin():
            c = await _company(s, a.slug)
            if a.delete:
                await secrets.delete_secret(s, c.id, a.name)
            else:
                await secrets.set_secret(s, c.id, a.name, value)
        print("Сохранено (зашифровано)" if not a.delete else "Удалено")
    elif a.cmd == "default-tasks":
        from app.db.models import ScheduledTask
        from app.services.tasks import create_default_tasks
        async with sm() as s, s.begin():
            c = await _company(s, a.slug)
            if await s.scalar(select(func.count()).select_from(ScheduledTask).where(ScheduledTask.company_id == c.id)):
                print("У компании уже есть задачи — ничего не делаю")
            else:
                print(f"Создано задач: {len(await create_default_tasks(s, c.id))}")
    elif a.cmd == "tasks":
        from app.db.models import ScheduledTask
        async with sm() as s:
            c = await _company(s, a.slug)
            for t in (await s.scalars(select(ScheduledTask).where(ScheduledTask.company_id == c.id))).all():
                print(f"{'on ' if t.enabled else 'off'} {t.kind:16} {t.title:32} next={t.next_run_at} "
                      f"last={t.last_status} {json.dumps(t.schedule, ensure_ascii=False)}")
    elif a.cmd == "run-task":
        from app.db.models import ScheduledTask
        from scripts.scheduler import build_deps
        from app.services.tasks import run_task
        async with sm() as s:
            c = await _company(s, a.slug)
            t = await s.scalar(select(ScheduledTask).where(ScheduledTask.company_id == c.id,
                                                           ScheduledTask.kind == a.kind))
        if t is None:
            raise SystemExit(f"Нет задачи {a.kind}")
        _, deps = build_deps()
        print(await run_task(sm, t.id, deps, force=True))
        async with sm() as s:
            print(json.dumps((await s.get(ScheduledTask, t.id)).last_result, ensure_ascii=False, default=str)[:3000])
    elif a.cmd == "report":
        from app.services import analytics
        async with sm() as s:
            c = await _company(s, a.slug)
            print(analytics.report_text(c.name, await analytics.period_report(s, c.id, days=a.days)))
    elif a.cmd == "export-openapi":
        from pathlib import Path

        from app.api.main import create_app
        Path(a.path).write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=1))
        print(f"OpenAPI → {a.path}")
    elif a.cmd == "list":
        async with sm() as s:
            for c in (await s.scalars(select(Company).order_by(Company.created_at))).all():
                docs = await s.scalar(select(func.count()).select_from(Document).where(Document.company_id == c.id))
                chunks = await s.scalar(select(func.count()).select_from(Chunk).where(Chunk.company_id == c.id))
                print(f"{c.slug}: {c.name} — документов {docs}, фрагментов {chunks}")
                for src in (await s.scalars(select(Source).where(Source.company_id == c.id))).all():
                    print(f"   {src.type:12} {src.status:8} {src.last_sync_at or '-'}  {src.ref}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("create-company"); p.add_argument("name"); p.add_argument("--slug")
    p = sub.add_parser("add-source"); p.add_argument("slug"); p.add_argument("type"); p.add_argument("ref")
    p.add_argument("--competitor", action="store_true"); p.add_argument("--options", help="JSON")
    p = sub.add_parser("sync"); p.add_argument("slug"); p.add_argument("--type")
    p = sub.add_parser("search"); p.add_argument("slug"); p.add_argument("query")
    p.add_argument("--limit", type=int, default=5); p.add_argument("--all", action="store_true",
                                                                   help="включая данные конкурентов")
    p = sub.add_parser("posts", help="последние сохранённые посты (от старых к новым)"); p.add_argument("slug")
    p.add_argument("--type", default="telegram_web"); p.add_argument("--limit", type=int, default=20)
    sub.add_parser("list")
    p = sub.add_parser("add-member"); p.add_argument("slug"); p.add_argument("telegram_id", type=int)
    p.add_argument("role", choices=["owner", "approver", "editor", "viewer"])
    p = sub.add_parser("settings", help="ключ.подключ=значение (JSON или строка); пустое значение удаляет ключ")
    p.add_argument("slug"); p.add_argument("pairs", nargs="*")
    p = sub.add_parser("secret"); p.add_argument("slug"); p.add_argument("name")
    p.add_argument("--delete", action="store_true")
    p = sub.add_parser("default-tasks"); p.add_argument("slug")
    p = sub.add_parser("tasks"); p.add_argument("slug")
    p = sub.add_parser("run-task"); p.add_argument("slug"); p.add_argument("kind")
    p = sub.add_parser("report"); p.add_argument("slug"); p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("export-openapi"); p.add_argument("path", nargs="?", default="docs/openapi.json")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
