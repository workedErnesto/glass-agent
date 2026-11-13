"""Компании, источники и синхронизация источников в PostgreSQL с индексацией RAG."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models import AuditLog, Company, Conversation, Document, Source
from app.parsers.base import ParsedDocument, SourceType, SyncResult, UpsertStatus
from app.rag.embeddings import Embedder
from app.rag.index import index_documents

log = logging.getLogger(__name__)
STALE_SYNC_S = 15 * 60  # «syncing» дольше 15 минут считается зависшим (процесс был прерван)
SOURCE_TYPES = {"website", "telegram", "telegram_web", "telegram_export", "vk", "telegram_bot", "file"}


def slugify(name: str) -> str:
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                          "abvgdeejziiklmnoprstufhccss_y_eua")
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(table)).strip("-")
    return s[:60] or f"company-{uuid.uuid4().hex[:8]}"


async def create_company(session: AsyncSession, name: str, *, slug: str | None = None,
                         profile: dict[str, Any] | None = None, user_id: int | None = None,
                         settings: dict[str, Any] | None = None, default_tasks: bool = True) -> Company:
    company = Company(name=name, slug=slug or slugify(name), profile=profile or {}, settings=settings or {})
    session.add(company)
    await session.flush()
    session.add(Conversation(company_id=company.id))  # собственный постоянный AI-диалог компании
    session.add(AuditLog(company_id=company.id, user_id=user_id, action="company.create", details={"name": name}))
    await session.flush()
    if default_tasks:
        from .tasks import create_default_tasks

        await create_default_tasks(session, company.id)  # отчёт, обновление источников, сбор результатов…
    return company


def normalize_source_ref(type_: str, ref: str) -> str:
    """Один канал = один источник: @salstek, t.me/s/salstek, https://t.me/salstek/10 → https://t.me/salstek."""
    if type_ == "telegram_web":
        from app.parsers.telegram_parser import parse_channel_ref

        kind, value = parse_channel_ref(ref)
        if kind == "username":
            return f"https://t.me/{value.lower()}"
    return ref.strip()


async def add_source(session: AsyncSession, company_id: uuid.UUID, type_: str, ref: str, *,
                     name: str = "", is_competitor: bool = False, options: dict[str, Any] | None = None,
                     user_id: int | None = None) -> Source:
    if type_ not in SOURCE_TYPES:
        raise ValueError(f"Неизвестный тип источника: {type_}")
    ref = normalize_source_ref(type_, ref)
    existing = await session.scalar(select(Source).where(Source.company_id == company_id, Source.type == type_,
                                                         Source.ref == ref))
    if existing:
        return existing
    src = Source(company_id=company_id, type=type_, ref=ref, name=name or ref, is_competitor=is_competitor,
                 options=options or {})
    session.add(src)
    await session.flush()
    session.add(AuditLog(company_id=company_id, user_id=user_id, action="source.add",
                         details={"type": type_, "ref": ref, "is_competitor": is_competitor}))
    return src


# --------------------------------------------------------------------------- хранилище для парсеров


def _row_to_parsed(row: Document) -> ParsedDocument:
    st, sid, ext = row.external_key.split(":", 2)
    return ParsedDocument(source_type=SourceType(st), source_id=sid, external_id=ext, url=row.url, title=row.title,
                          text=row.text, published_at=row.published_at, edited_at=row.edited_at,
                          fetched_at=row.fetched_at or datetime.now(timezone.utc), content_hash=row.content_hash,
                          metrics=row.metrics or {}, metadata=row.meta or {})


class PgSourceStore:
    """Снимок документов одного источника в памяти с интерфейсом JsonlDocumentStore.

    Парсер работает с ним синхронно (сеть долгая — транзакцию не держим),
    затем `flush()` записывает изменения в PostgreSQL одной транзакцией."""

    def __init__(self, source: Source, rows: list[Document]):
        self.source_id = source.id
        self.company_id = source.company_id
        self.is_competitor = source.is_competitor
        self._docs: dict[str, ParsedDocument] = {r.external_key: _row_to_parsed(r) for r in rows}
        self._dirty: dict[str, None] = {}  # dict сохраняет порядок записи (от старых к новым)
        self._metrics_only: dict[str, None] = {}
        self._removed: set[str] = set()
        self.state: dict[str, Any] = dict(source.sync_state or {})

    def __len__(self) -> int:
        return len(self._docs)

    def get(self, key: str) -> ParsedDocument | None:
        return self._docs.get(key)

    def all(self, source_type: SourceType | None = None, source_id: str | None = None) -> list[ParsedDocument]:
        return [d for d in self._docs.values() if (source_type is None or d.source_type == source_type)
                and (source_id is None or d.source_id == source_id)]

    def upsert(self, doc: ParsedDocument) -> UpsertStatus:
        old = self._docs.get(doc.key)
        self._removed.discard(doc.key)
        if old is None or old.content_hash != doc.content_hash:
            self._docs[doc.key] = doc
            self._dirty[doc.key] = None
            return UpsertStatus.NEW if old is None else UpsertStatus.UPDATED
        if old.metrics != doc.metrics:
            old.metrics = doc.metrics
            old.fetched_at = doc.fetched_at
            self._metrics_only[doc.key] = None
            return UpsertStatus.METRICS
        return UpsertStatus.UNCHANGED

    def remove(self, keys) -> int:
        n = 0
        for k in keys:
            if self._docs.pop(k, None) is not None:
                self._removed.add(k)
                self._dirty.pop(k, None)
                n += 1
        return n

    def find_by_hash(self, source_type: SourceType, source_id: str, digest: str) -> ParsedDocument | None:
        return next((d for d in self._docs.values() if d.source_type == source_type and d.source_id == source_id
                     and d.content_hash == digest), None)

    def get_state(self, name: str) -> dict[str, Any]:
        return dict(self.state.get(name, {}))

    def set_state(self, name: str, state: dict[str, Any]) -> None:
        self.state[name] = state

    def save(self) -> None:  # запись выполняет flush()
        pass

    async def flush(self, session: AsyncSession) -> list[Document]:
        keys = list(self._dirty) + [k for k in self._metrics_only if k not in self._dirty]
        existing = {r.external_key: r for r in (await session.scalars(
            select(Document).where(Document.source_id == self.source_id, Document.external_key.in_(keys))
        )).all()} if keys else {}
        changed: list[Document] = []
        for key in keys:
            d = self._docs[key]
            row = existing.get(key)
            if row is None:
                row = Document(company_id=self.company_id, source_id=self.source_id, external_key=key,
                               is_competitor=self.is_competitor)
                session.add(row)
            row.source_type = d.source_type.value
            row.url, row.title, row.text = d.url, d.title, d.text
            row.content_hash = d.content_hash
            row.published_at, row.edited_at, row.fetched_at = d.published_at, d.edited_at, d.fetched_at
            row.metrics, row.meta = d.metrics, d.metadata
            if key in self._dirty:
                changed.append(row)
        if self._removed:
            await session.execute(delete(Document).where(Document.source_id == self.source_id,
                                                         Document.external_key.in_(self._removed)))
        await session.flush()
        return changed


# --------------------------------------------------------------------------- синхронизация


async def _run_parser(src: Source, store: PgSourceStore) -> SyncResult:
    s = get_settings()
    opts = src.options or {}
    if src.type == "website":
        from app.parsers.website_parser import WebsiteParser, WebsiteParserConfig

        cfg = WebsiteParserConfig(max_pages=int(opts.get("max_pages", 300)), max_depth=int(opts.get("max_depth", 4)),
                                  allowed_languages=tuple(opts.get("languages", ["ru"])))
        return await WebsiteParser(cfg).sync(src.ref, store)  # type: ignore[arg-type]
    if src.type == "telegram":
        from app.parsers.telegram_parser import TelethonChannelParser

        p = TelethonChannelParser(s.tg_api_id, s.tg_api_hash, s.tg_session_path)
        return await p.sync(src.ref, store, limit=opts.get("limit"), join=bool(opts.get("join")),  # type: ignore[arg-type]
                            refresh_recent=int(opts.get("refresh_recent", 50)))
    if src.type == "telegram_web":
        from app.parsers.telegram_nokey import TelegramWebPreviewParser

        return await TelegramWebPreviewParser().sync(
            src.ref, store, max_posts=int(opts.get("max_posts", 1000)),  # type: ignore[arg-type]
            initial_pages=int(opts.get("initial_pages", s.tg_web_initial_pages)))
    if src.type == "telegram_export":
        from app.parsers.telegram_nokey import TelegramExportImporter

        return await TelegramExportImporter().sync(src.ref, store)  # type: ignore[arg-type]
    if src.type == "telegram_bot":
        from app.parsers.telegram_parser import TelegramBotChannelReader

        return await TelegramBotChannelReader(s.tg_bot_token).sync(store)  # type: ignore[arg-type]
    if src.type == "vk":
        from app.parsers.vk_parser import VKParser

        p = VKParser(s.vk_access_token, api_version=s.vk_api_version, api_base=s.vk_api_base)
        return await p.sync(src.ref, store, max_posts=int(opts.get("max_posts", 1000)),  # type: ignore[arg-type]
                            refresh_recent=int(opts.get("refresh_recent", 100)))
    if src.type == "file":
        from app.parsers.file_parser import FileParser

        return await FileParser().sync(src.ref, store)  # type: ignore[arg-type]
    raise ValueError(f"Нет парсера для {src.type}")


async def sync_source(sessionmaker: async_sessionmaker[AsyncSession], source_id: uuid.UUID, embedder: Embedder,
                      *, company_id: uuid.UUID | None = None) -> SyncResult:
    async with sessionmaker() as session, session.begin():
        q = select(Source).where(Source.id == source_id)
        if company_id is not None:
            q = q.where(Source.company_id == company_id)
        src = await session.scalar(q.with_for_update())
        if src is None:
            raise LookupError("Источник не найден")
        stale = src.updated_at and (datetime.now(timezone.utc) - src.updated_at).total_seconds() > STALE_SYNC_S
        if src.status == "syncing" and not stale:
            raise RuntimeError("Синхронизация этого источника уже выполняется")
        src.status = "syncing"
        rows = (await session.scalars(select(Document).where(Document.source_id == src.id))).all()
        store = PgSourceStore(src, list(rows))

    try:
        result = await _run_parser(src, store)
    except Exception as e:
        async with sessionmaker() as session, session.begin():
            row = await session.get(Source, source_id)
            row.status, row.last_error = "error", f"{type(e).__name__}: {e}"
        raise

    async with sessionmaker() as session, session.begin():
        row = await session.get(Source, source_id)
        changed = await store.flush(session)
        indexed = await index_documents(session, embedder, changed)
        result.notes.append(f"RAG: проиндексировано фрагментов {indexed} в {len(changed)} документах")
        row.sync_state = store.state
        row.status = "ok" if not result.errors else "error"
        row.last_error = "; ".join(result.errors[:5]) or None
        row.last_sync_at = datetime.now(timezone.utc)
        row.last_result = result.model_dump(mode="json")
        session.add(AuditLog(company_id=row.company_id, action="source.sync",
                             details={"source_id": str(row.id), "new": result.new, "updated": result.updated,
                                      "errors": len(result.errors)}))
    return result
