"""Alembic: миграции схемы. Создать новую после изменения app/db/models.py:

    .venv/bin/alembic revision --autogenerate -m "что изменилось"
    .venv/bin/alembic upgrade head        # или просто запуск API/бота/планировщика (init_db)
"""
import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _include(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True,
                      compare_type=True, include_object=_include)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      include_object=_include)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = config.attributes.get("url") or get_settings().database_url
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # вызов из приложения (init_db) — соединение уже открыто
        do_run_migrations(connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
