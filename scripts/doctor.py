"""Проверка окружения Glass Agent: что настроено, что нет и как исправить.

    python -m scripts.doctor            # локальные проверки (без запросов к внешним сервисам)
    python -m scripts.doctor --live     # + запросы только на чтение: Telegram getMe/getChat, ChatGPT /models,
                                        #   VK groups.getById, MAX /me
    python -m scripts.doctor --model    # + загрузка модели эмбеддингов
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import func, select, text

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
results: list[tuple[str, str, str, str]] = []


def report(status: str, area: str, msg: str, fix: str = "") -> None:
    results.append((status, area, msg, fix))
    print(f"{status} {area}: {msg}" + (f"\n      → {fix}" if fix else ""))


async def check_db() -> bool:
    from app.db.session import get_engine

    try:
        async with get_engine().connect() as c:
            ext = await c.scalar(text("select extversion from pg_extension where extname='vector'"))
            ver = await c.scalar(text("select version_num from alembic_version")) if await c.scalar(
                text("select to_regclass('alembic_version')")) else None
    except Exception as e:  # noqa: BLE001
        report(FAIL, "PostgreSQL", f"нет подключения: {type(e).__name__}", "docker compose up -d db; DATABASE_URL в .env")
        return False
    report(OK if ext else FAIL, "pgvector", f"версия {ext}" if ext else "расширение не установлено",
           "" if ext else "образ pgvector/pgvector:pg16")
    from alembic.script import ScriptDirectory

    from app.db.session import _alembic_config
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    report(OK if ver == head else WARN, "Миграции", f"ревизия БД {ver}, последняя {head}",
           "" if ver == head else "python -m scripts.manage init-db (или alembic upgrade head)")
    return True


async def check_companies() -> None:
    from app.db.models import Company, CompanyMember, ScheduledTask, Source
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        companies = (await s.scalars(select(Company))).all()
        if not companies:
            report(WARN, "Компании", "нет ни одной", "бот: /newcompany Название (админ) или POST /api/companies")
        for c in companies:
            ch = (c.settings or {}).get("channels") or {}
            if (c.settings or {}).get("telegram_channel"):
                ch = {"telegram": c.settings["telegram_channel"], **ch}
            members = dict((await s.execute(select(CompanyMember.role, func.count()).where(
                CompanyMember.company_id == c.id).group_by(CompanyMember.role))).all())
            tasks = await s.scalar(select(func.count()).select_from(ScheduledTask).where(ScheduledTask.company_id == c.id))
            sources = await s.scalar(select(func.count()).select_from(Source).where(Source.company_id == c.id))
            problems = []
            if not ch:
                problems.append(f"нет канала публикации: python -m scripts.manage settings {c.slug} "
                                "channels.telegram=@канал")
            if not (members.get("owner") or members.get("approver")):
                problems.append(f"нет согласующих: python -m scripts.manage add-member {c.slug} <telegram_id> owner")
            if not tasks:
                problems.append(f"нет регулярных задач: python -m scripts.manage default-tasks {c.slug}")
            report(OK if not problems else WARN, f"Компания {c.slug}",
                   f"источников {sources}, задач {tasks}, участники {members or '—'}, каналы {ch or '—'}",
                   "; ".join(problems))


def check_fonts() -> None:
    from app.services.images import ImageError, find_font

    try:
        report(OK, "Шрифты", find_font(True))
    except ImageError as e:
        report(FAIL, "Шрифты", str(e), "apt install fonts-dejavu-core или GLASS_FONT=/путь/к/шрифту.ttf")


async def check_ai(live: bool) -> None:
    from app.ai.factory import get_ai_connector
    from app.core.config import get_settings

    s = get_settings()
    if s.ai_provider == "mock_dev":
        report(WARN, "AI", "AI_PROVIDER=mock_dev — ТЕСТОВАЯ модель, ответы не от ChatGPT", "убрать AI_PROVIDER из .env")
        return
    conn = get_ai_connector()
    st = await conn.status()
    if not st.get("connected"):
        report(FAIL, "ChatGPT Plus", "не подключён — чат и генерация отвечают AI_CONNECTION_UNAVAILABLE",
               "python -m scripts.chatgpt_login (браузер на этой машине); для VM — docs/DEPLOY.md")
        return
    if not st.get("plan_usage_permission"):
        report(FAIL, "ChatGPT Plus", "нет разрешения chatgpt.tokens.use.direct", "войти заново: python -m scripts.chatgpt_login")
        return
    report(OK, "ChatGPT Plus", f"подключён, токен до {st.get('expires_at')}")
    if live:
        try:
            models = await conn.list_models()
            report(OK, "ChatGPT модели", ", ".join(m["slug"] for m in models[:8]) or "—")
        except Exception as e:  # noqa: BLE001
            report(FAIL, "ChatGPT модели", f"{type(e).__name__}: {e}", "python -m scripts.chatgpt_login --status")


async def check_telegram(live: bool) -> None:
    from app.core.config import get_settings
    from app.db.models import Company
    from app.db.session import get_sessionmaker
    from app.services.publisher import PublishError, TelegramBotAPI

    s = get_settings()
    if not s.tg_bot_token:
        report(FAIL, "Telegram бот", "TG_BOT_TOKEN не задан", "создать бота у @BotFather и вписать токен в .env")
        return
    if not s.admin_ids:
        report(WARN, "Администраторы", "ADMIN_TELEGRAM_IDS пуст — некому подключать компании и ChatGPT",
               "узнать свой ID командой /start у бота и вписать в .env")
    if not live:
        report(OK, "Telegram бот", "токен задан (проверка доступа: --live)")
        return
    api = TelegramBotAPI(s.tg_bot_token)
    try:
        me = await api.call("getMe")
    except PublishError as e:
        report(FAIL, "Telegram бот", str(e), "проверить TG_BOT_TOKEN")
        return
    report(OK, "Telegram бот", f"@{me.get('username')} (id {me.get('id')})")
    async with get_sessionmaker()() as sess:
        for c in (await sess.scalars(select(Company))).all():
            settings = c.settings or {}
            target = (settings.get("channels") or {}).get("telegram") or settings.get("telegram_channel")
            if not target:
                continue
            try:
                chat = await api.call("getChat", {"chat_id": target})
                member = await api.call("getChatMember", {"chat_id": chat["id"], "user_id": me["id"]})
            except PublishError as e:
                report(FAIL, f"Канал {c.slug}", f"{target}: {e}", "добавить бота в канал администратором")
                continue
            can_post = member.get("status") == "creator" or member.get("can_post_messages")
            report(OK if can_post else FAIL, f"Канал {c.slug}", f"{chat.get('title')} — бот {member.get('status')}"
                   f", публикация {'разрешена' if can_post else 'ЗАПРЕЩЕНА'}",
                   "" if can_post else "в настройках канала дать боту право «Публикация сообщений»")


async def check_vk(live: bool) -> None:
    from app.core.config import get_settings

    s = get_settings()
    if not s.vk_app_id:
        report(WARN, "VK", "VK_APP_ID не задан — ключ по ссылке получить нельзя", "docs/HANDOFF.md, раздел VK")
    report(OK if s.vk_access_token else WARN, "VK чтение", "VK_ACCESS_TOKEN задан" if s.vk_access_token
           else "нет ключа — парсер VK и сбор комментариев не работают", "" if s.vk_access_token
           else "python -m scripts.vk_token")
    report(OK if s.vk_publish_token else WARN, "VK публикация", "VK_PUBLISH_TOKEN задан" if s.vk_publish_token
           else "нет общего ключа публикации (можно секретом компании vk_token)",
           "" if s.vk_publish_token else "python -m scripts.manage secret <slug> vk_token")
    if live and s.vk_access_token:
        import httpx

        from app.parsers.vk_parser import VKError, VKParser
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                g = await VKParser(s.vk_access_token, api_base=s.vk_api_base).get_group(client, "salavatsteklo")
            report(OK, "VK API", f"доступ есть: {g.get('name')}")
        except VKError as e:
            report(FAIL, "VK API", str(e), "получить новый ключ: python -m scripts.vk_token")


async def check_max(live: bool) -> None:
    from app.core.config import get_settings

    s = get_settings()
    if not s.max_bot_token:
        report(WARN, "MAX", "MAX_BOT_TOKEN не задан — публикация в MAX недоступна",
               "business.max.ru → Чат-боты → Настройки → токен; или секрет компании max_bot_token")
        return
    if live:
        from app.services.platforms import MaxAPI
        from app.services.publisher import PublishError
        try:
            me = await MaxAPI(s.max_bot_token.get_secret_value(), api_base=s.max_api_base).request("GET", "/me")
            report(OK, "MAX бот", f"{me.get('first_name')} (@{me.get('username')})")
        except PublishError as e:
            report(FAIL, "MAX бот", str(e), "проверить MAX_BOT_TOKEN")
    else:
        report(OK, "MAX", "токен задан (проверка: --live)")


def check_model() -> None:
    from app.rag.embeddings import get_embedder

    try:
        vec = asyncio.run(get_embedder().embed_query("проверка"))
        report(OK, "Эмбеддинги", f"модель загружена, размерность {len(vec)}")
    except Exception as e:  # noqa: BLE001
        report(FAIL, "Эмбеддинги", f"{type(e).__name__}: {e}", "pip install sentence-transformers; доступ к huggingface.co")


async def main_async(a) -> int:
    print("Glass Agent — проверка окружения\n")
    if not Path(".env").exists():
        report(WARN, ".env", "файл не найден", "cp .env.example .env")
    if await check_db():
        await check_companies()
    check_fonts()
    await check_ai(a.live)
    await check_telegram(a.live)
    await check_vk(a.live)
    await check_max(a.live)
    fails = sum(1 for r in results if r[0] == FAIL)
    warns = sum(1 for r in results if r[0] == WARN)
    print(f"\nИтого: ошибок {fails}, предупреждений {warns}")
    return 1 if fails else 0


def main() -> int:
    import logging
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="запросы на чтение к внешним сервисам")
    ap.add_argument("--model", action="store_true", help="проверить загрузку модели эмбеддингов")
    a = ap.parse_args()
    code = asyncio.run(main_async(a))
    if a.model:
        check_model()
    return code


if __name__ == "__main__":
    sys.exit(main())
