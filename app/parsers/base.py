"""Общие модели и хранилище для парсеров.

Каждый парсер возвращает `ParsedDocument` — нормализованную единицу знаний
(страница сайта, пост Telegram, пост VK) с указанием источника.

`JsonlDocumentStore` — временное файловое хранилище с дедупликацией и
инкрементальным состоянием. Интерфейс `DocumentSink` позволяет позже заменить
его репозиторием PostgreSQL без изменения парсеров.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Protocol

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    WEBSITE = "website"
    TELEGRAM = "telegram"
    VK = "vk"
    FILE = "file"


class UpsertStatus(str, Enum):
    NEW = "new"
    UPDATED = "updated"  # изменился текст / содержимое
    METRICS = "metrics"  # изменились только показатели (просмотры, лайки...)
    UNCHANGED = "unchanged"


_WS_RE = re.compile(r"[ \t ]+")
_MANY_NL_RE = re.compile(r"\n{3,}")
_HASHTAG_RE = re.compile(r"(?<![\w#])#([\w\d_]{2,64})", re.UNICODE)


def clean_text(text: str | None) -> str:
    """Нормализует пробелы и переводы строк, убирает управляющие символы."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return _MANY_NL_RE.sub("\n\n", text).strip()


def extract_hashtags(text: str | None) -> list[str]:
    if not text:
        return []
    seen: dict[str, None] = {}
    for tag in _HASHTAG_RE.findall(text):
        seen.setdefault(tag.lower(), None)
    return list(seen)


_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def make_title(text: str | None, media: list[str] | None = None, limit: int = 120) -> str:
    """Заголовок поста: первая строка, где есть хотя бы 3 буквы (а не только эмодзи)."""
    for line in (text or "").splitlines():
        if len(_LETTERS.findall(line)) >= 3:
            return line.strip()[:limit]
    if text and text.strip():
        return text.strip().splitlines()[0][:limit]
    return f"[{', '.join(media or [])}]"


def content_hash(*parts: str | None) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(clean_text(p).lower().encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ParsedDocument(BaseModel):
    """Нормализованный документ из любого источника."""

    source_type: SourceType
    source_id: str  # идентификатор источника: домен сайта, id канала, id сообщества
    external_id: str  # уникальный id внутри источника: URL, id поста
    url: str  # ссылка на оригинал (для цитирования в RAG)
    title: str = ""
    text: str = ""
    published_at: datetime | None = None
    edited_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    content_hash: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)  # views, likes, ...
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source_type.value}:{self.source_id}:{self.external_id}"

    def model_post_init(self, __context: Any) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.title, self.text)


class SyncResult(BaseModel):
    source_type: SourceType
    source_id: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    new: int = 0
    updated: int = 0
    metrics_updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def count(self, status: UpsertStatus) -> None:
        if status is UpsertStatus.NEW:
            self.new += 1
        elif status is UpsertStatus.UPDATED:
            self.updated += 1
        elif status is UpsertStatus.METRICS:
            self.metrics_updated += 1
        else:
            self.unchanged += 1

    @property
    def changed(self) -> int:
        return self.new + self.updated


# Хук для запуска индексации RAG по изменённым документам.
IndexCallback = Callable[[list[ParsedDocument]], Awaitable[None]]


class DocumentSink(Protocol):
    """Куда парсер складывает документы. Реализации: JSONL сейчас, PostgreSQL позже."""

    def upsert(self, doc: ParsedDocument) -> UpsertStatus: ...
    def find_by_hash(self, source_type: SourceType, source_id: str, digest: str) -> ParsedDocument | None: ...
    def get_state(self, name: str) -> dict[str, Any]: ...
    def set_state(self, name: str, state: dict[str, Any]) -> None: ...
    def save(self) -> None: ...


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class JsonlDocumentStore:
    """Файловое хранилище: documents.jsonl + state.json в каталоге компании.

    Дедупликация по ключу документа (источник + external_id); изменение
    определяется по content_hash, показатели обновляются отдельно.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.docs_path = self.directory / "documents.jsonl"
        self.state_path = self.directory / "state.json"
        self._docs: dict[str, ParsedDocument] = {}
        self._state: dict[str, dict[str, Any]] = {}
        if self.docs_path.exists():
            for line in self.docs_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    doc = ParsedDocument.model_validate_json(line)
                    self._docs[doc.key] = doc
        if self.state_path.exists():
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))

    def __len__(self) -> int:
        return len(self._docs)

    def all(self, source_type: SourceType | None = None, source_id: str | None = None) -> list[ParsedDocument]:
        return [
            d
            for d in self._docs.values()
            if (source_type is None or d.source_type == source_type)
            and (source_id is None or d.source_id == source_id)
        ]

    def get(self, key: str) -> ParsedDocument | None:
        return self._docs.get(key)

    def upsert(self, doc: ParsedDocument) -> UpsertStatus:
        old = self._docs.get(doc.key)
        if old is None:
            self._docs[doc.key] = doc
            return UpsertStatus.NEW
        if old.content_hash != doc.content_hash:
            self._docs[doc.key] = doc
            return UpsertStatus.UPDATED
        if old.metrics != doc.metrics:
            old.metrics = doc.metrics
            old.fetched_at = doc.fetched_at
            return UpsertStatus.METRICS
        old.fetched_at = doc.fetched_at
        return UpsertStatus.UNCHANGED

    def remove(self, keys: Iterable[str]) -> int:
        n = 0
        for k in keys:
            if self._docs.pop(k, None) is not None:
                n += 1
        return n

    def find_by_hash(self, source_type: SourceType, source_id: str, digest: str) -> ParsedDocument | None:
        for d in self._docs.values():
            if d.source_type == source_type and d.source_id == source_id and d.content_hash == digest:
                return d
        return None

    def get_state(self, name: str) -> dict[str, Any]:
        return dict(self._state.get(name, {}))

    def set_state(self, name: str, state: dict[str, Any]) -> None:
        self._state[name] = state

    def save(self) -> None:
        lines = [d.model_dump_json() for d in sorted(self._docs.values(), key=lambda d: d.key)]
        _atomic_write(self.docs_path, "\n".join(lines) + ("\n" if lines else ""))
        _atomic_write(self.state_path, json.dumps(self._state, ensure_ascii=False, indent=2, default=str))
