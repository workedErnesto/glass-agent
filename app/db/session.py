"""Подключение к PostgreSQL и применение миграций (Alembic).

init_db() вызывается при старте API, бота, планировщика и CLI:
* новая база — `alembic upgrade head`;
* база, созданная ранней версией через create_all (без alembic_version), — догоняется
  create_all + ALTER и помечается текущей ревизией (stamp), дальше обычные миграции.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

from .models import Base

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]

# Колонки, добавленные после первых версий без Alembic (для баз, созданных create_all)
_LEGACY_ALTERS = [
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS state JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS prefs JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS proposed_at TIMESTAMPTZ",
]


@lru_cache
def get_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(url or get_settings().database_url, pool_pre_ping=True)


def get_sessionmaker(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine or get_engine(), expire_on_commit=False)


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def _migrate(sync_conn) -> None:
    from alembic import command

    cfg = _alembic_config()
    cfg.attributes["connection"] = sync_conn
    tables = set(inspect(sync_conn).get_table_names())
    if "alembic_version" not in tables and "companies" in tables:
        log.warning("База без истории миграций: догоняю схему и помечаю текущей ревизией")
        Base.metadata.create_all(sync_conn)
        for stmt in _LEGACY_ALTERS:
            sync_conn.execute(text(stmt))
        command.stamp(cfg, "head")
        return
    command.upgrade(cfg, "head")


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Расширение pgvector и миграции до последней ревизии."""
    engine = engine or get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(_migrate)
