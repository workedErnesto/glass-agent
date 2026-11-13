"""Документы компании: PDF / DOCX / TXT / MD → база знаний.

Источник — файл или каталог. Повторная синхронизация: новые и изменённые
файлы переиндексируются, удалённые — убираются, одинаковое содержимое под
разными именами считается дублем.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .base import DocumentSink, ParsedDocument, SourceType, SyncResult, clean_text, content_hash, utcnow

log = logging.getLogger(__name__)
SUPPORTED = {".pdf", ".docx", ".txt", ".md"}
MAX_FILE_MB = 50


class FileParseError(RuntimeError):
    pass


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise FileParseError(f"Файл больше {MAX_FILE_MB} МБ")
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise FileParseError("PDF защищён паролем") from e
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(t for t in pages if t.strip())
        if not text.strip():
            raise FileParseError("В PDF нет текстового слоя (скан) — нужен OCR")
        return clean_text(text)
    if ext == ".docx":
        import docx

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:  # таблицы характеристик → строки "ячейка | ячейка"
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(dict.fromkeys(cells)))
        return clean_text("\n\n".join(parts))
    if ext in (".txt", ".md"):
        raw = path.read_bytes()
        for enc in ("utf-8", "cp1251"):
            try:
                return clean_text(raw.decode(enc))
            except UnicodeDecodeError:
                continue
        raise FileParseError("Неизвестная кодировка текста")
    raise FileParseError(f"Формат {ext} не поддерживается")


class FileParser:
    async def sync(self, ref: str, store: DocumentSink) -> SyncResult:
        root = Path(ref).expanduser()
        source_id = root.name if root.is_dir() else root.parent.name or "files"
        result = SyncResult(source_type=SourceType.FILE, source_id=source_id)
        if not root.exists():
            result.errors.append(f"Путь не найден: {root}")
            result.finished_at = utcnow()
            return result
        files = sorted(p for p in (root.rglob("*") if root.is_dir() else [root])
                       if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("."))
        seen: set[str] = set()
        for f in files:
            rel = str(f.relative_to(root)) if root.is_dir() else f.name
            try:
                text = extract_text(f)
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"{rel}: {e}")
                continue
            title = f.stem.replace("_", " ")
            digest = content_hash(text)  # дубль = то же содержимое под другим именем
            dup = store.find_by_hash(SourceType.FILE, source_id, digest)
            key = f"file:{source_id}:{rel}"
            if dup is not None and dup.key != key:
                result.duplicates += 1
                seen.add(dup.key)
                continue
            doc = ParsedDocument(source_type=SourceType.FILE, source_id=source_id, external_id=rel,
                                 url=f"file://{rel}", title=title, text=text, content_hash=digest,
                                 published_at=datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc),
                                 metadata={"filename": f.name, "ext": f.suffix.lower(), "size": f.stat().st_size})
            seen.add(doc.key)
            result.count(store.upsert(doc))
        if hasattr(store, "all") and hasattr(store, "remove"):
            gone = [d.key for d in store.all(SourceType.FILE, source_id) if d.key not in seen]  # type: ignore[attr-defined]
            if gone:
                store.remove(gone)  # type: ignore[attr-defined]
                result.notes.append(f"Удалено документов отсутствующих файлов: {len(gone)}")
        store.save()
        result.finished_at = utcnow()
        return result
