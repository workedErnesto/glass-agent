"""PostgreSQL + RAG: изоляция компаний, инкрементальная индексация, гибридный поиск.

Нужен PostgreSQL с pgvector (docker compose up -d db), база glass_test.
Эмбеддер ТЕСТОВЫЙ (хеширование слов), чтобы не загружать модель; реальная
модель e5 проверяется вручную: python -m scripts.manage search ...
"""
import hashlib
import math
import os
import re

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Chunk, Document
from app.parsers.base import ParsedDocument, SourceType, SyncResult
from app.rag.chunking import chunk_text
from app.rag.index import HybridSearcher, index_documents, keyword_tsquery, verify_quote
from app.services import sources as svc

DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://glass:glass@127.0.0.1:5433/glass_test")


class HashEmbedder:
    """ТЕСТОВЫЙ эмбеддер: мешок слов → 384-мерный нормализованный вектор."""
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


@pytest_asyncio.fixture
async def sm():
    engine = create_async_engine(DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except OSError as e:
        pytest.skip(f"PostgreSQL недоступен: {e}")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def pdoc(ext, text, title="T", metrics=None):
    return ParsedDocument(source_type=SourceType.WEBSITE, source_id="x.ru", external_id=ext, url=f"https://x.ru/{ext}",
                          title=title, text=text, metrics=metrics or {})


def test_chunking_and_tsquery():
    chunks = chunk_text("Абзац один. " * 100 + "\n\nАбзац два.", title="Заголовок", max_chars=300)
    assert all(c.startswith("Заголовок") for c in chunks) and all(len(c) <= 330 for c in chunks)
    assert keyword_tsquery("Стекло & 'drop table' | !") == "стекло | drop | table"


async def test_company_isolation_and_competitors(sm):
    emb = HashEmbedder()
    async with sm() as s, s.begin():
        a = await svc.create_company(s, "Компания А", slug="a")
        b = await svc.create_company(s, "Компания Б", slug="b")
        sa = await svc.add_source(s, a.id, "website", "https://a.ru/")
        sc = await svc.add_source(s, a.id, "website", "https://rival.ru/", is_competitor=True)
        sb = await svc.add_source(s, b.id, "website", "https://b.ru/")
        docs = [
            Document(company_id=a.id, source_id=sa.id, source_type="website", external_key="1", url="https://a.ru/1",
                     title="Закалённое стекло", text="Мы производим закалённое стекло для фасадов", content_hash="h1"),
            Document(company_id=a.id, source_id=sc.id, source_type="website", external_key="2", url="https://rival.ru/2",
                     title="Конкурент", text="Конкурент продаёт закалённое стекло дешевле", content_hash="h2",
                     is_competitor=True),
            Document(company_id=b.id, source_id=sb.id, source_type="website", external_key="3", url="https://b.ru/3",
                     title="Секрет Б", text="Компания Б тоже делает закалённое стекло", content_hash="h3"),
        ]
        s.add_all(docs)
        await s.flush()
        assert await index_documents(s, emb, docs) == 3
        assert await index_documents(s, emb, docs) == 0  # без изменений — без переиндексации
    searcher = HybridSearcher(emb)
    async with sm() as s, s.begin():
        own = await searcher.search(s, a.id, "закалённое стекло")
        assert {h.url for h in own} == {"https://a.ru/1"}
        both = await searcher.search(s, a.id, "закалённое стекло", competitor=None)
        assert {h.url for h in both} == {"https://a.ru/1", "https://rival.ru/2"}
        rival = await searcher.search(s, a.id, "закалённое стекло", competitor=True)
        assert {h.url for h in rival} == {"https://rival.ru/2"}
        assert all("b.ru" not in h.url for h in both)  # данные компании Б не видны
        assert own[0].semantic_rank == 1 and own[0].keyword_rank == 1
        assert await verify_quote(s, a.id, own[0].document_id, "производим   ЗАКАЛЁННОЕ стекло")
        assert not await verify_quote(s, b.id, own[0].document_id, "производим")  # чужая компания


async def test_sync_source_incremental(sm, monkeypatch):
    pages = {"a": pdoc("a", "Стеклопакеты энергосберегающие " * 5), "b": pdoc("b", "Зеркала Mirroline " * 5)}

    async def fake_parser(src, store):
        r = SyncResult(source_type=SourceType.WEBSITE, source_id="x.ru")
        seen = set()
        for d in pages.values():
            r.count(store.upsert(d.model_copy(deep=True)))
            seen.add(d.key)
        store.remove([d.key for d in store.all() if d.key not in seen])
        store.set_state("website:x.ru", {"pages": len(pages)})
        return r

    monkeypatch.setattr(svc, "_run_parser", fake_parser)
    emb = HashEmbedder()
    async with sm() as s, s.begin():
        c = await svc.create_company(s, "C", slug="c")
        src = await svc.add_source(s, c.id, "website", "https://x.ru/")

    r1 = await svc.sync_source(sm, src.id, emb, company_id=c.id)
    assert r1.new == 2 and "фрагментов 2" in r1.notes[-1]

    pages["a"] = pdoc("a", "Стеклопакеты энергосберегающие НОВЫЕ " * 5)
    del pages["b"]
    r2 = await svc.sync_source(sm, src.id, emb, company_id=c.id)
    assert (r2.new, r2.updated) == (0, 1) and "в 1 документах" in r2.notes[-1]
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(Document)) == 1
        assert await s.scalar(select(func.count()).select_from(Chunk)) == 1  # чанки удалённой страницы удалены
        row = await s.get(type(src), src.id)
        assert row.status == "ok" and row.sync_state == {"website:x.ru": {"pages": 1}}

    with pytest.raises(LookupError):  # чужая компания не может синхронизировать источник
        async with sm() as s, s.begin():
            other = await svc.create_company(s, "Other", slug="other")
        await svc.sync_source(sm, src.id, emb, company_id=other.id)
