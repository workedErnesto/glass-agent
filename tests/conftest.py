"""Общие фикстуры: тестовая БД PostgreSQL (glass_test) и ТЕСТОВЫЙ эмбеддер."""
import hashlib
import math
import os
import re

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://glass:glass@127.0.0.1:5433/glass_test")


class HashEmbedder:
    """ТЕСТОВЫЙ эмбеддер: мешок слов → 384-мерный нормализованный вектор (без загрузки модели)."""
    dim = 384

    def _vec(self, text):
        v = [0.0] * 384
        for w in re.findall(r"\w{3,}", text.lower()):
            v[int(hashlib.md5(w[:6].encode()).hexdigest(), 16) % 384] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        return [x / n for x in v]

    async def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    async def embed_query(self, text):
        return self._vec(text)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    """Тесты никогда не используют настоящие токены из .env (никаких запросов к реальным Telegram/VK/MAX/ChatGPT)."""
    from app.core.config import get_settings

    s = get_settings()
    for name in ("tg_bot_token", "vk_access_token", "vk_publish_token", "max_bot_token", "tg_api_hash"):
        monkeypatch.setattr(s, name, None)
    monkeypatch.setattr(s, "ai_provider", "chatgpt_plus")


@pytest_asyncio.fixture
async def pg():
    """Чистая схема в glass_test; возвращает (engine, sessionmaker)."""
    engine = create_async_engine(TEST_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except OSError as e:
        pytest.skip(f"PostgreSQL недоступен: {e}")
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
