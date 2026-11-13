"""Индексация документов и гибридный поиск (pgvector + полнотекстовый поиск PostgreSQL, RRF).

Все запросы фильтруются по company_id, переданному сервером — знания разных
компаний не смешиваются. Собственные и конкурентные данные различаются
флагом is_competitor.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document

from .chunking import chunk_text
from .embeddings import Embedder

log = logging.getLogger(__name__)
RRF_K = 60


async def index_documents(session: AsyncSession, embedder: Embedder, docs: Sequence[Document], *,
                          force: bool = False) -> int:
    """(Пере)строит чанки документов, у которых изменился content_hash. Возвращает число чанков."""
    todo = [d for d in docs if force or d.indexed_hash != d.content_hash]
    if not todo:
        return 0
    await session.execute(delete(Chunk).where(Chunk.document_id.in_([d.id for d in todo])))
    plan: list[tuple[Document, int, str]] = []
    for d in todo:
        for i, c in enumerate(chunk_text(d.text, title=d.title)):
            plan.append((d, i, c))
    total = 0
    batch = 256
    for start in range(0, len(plan), batch):
        part = plan[start:start + batch]
        vectors = await embedder.embed_passages([c for _, _, c in part])
        session.add_all([
            Chunk(company_id=d.company_id, document_id=d.id, chunk_index=i, text=c, embedding=v,
                  is_competitor=d.is_competitor)
            for (d, i, c), v in zip(part, vectors)
        ])
        total += len(part)
    for d in todo:
        d.indexed_hash = d.content_hash
    await session.flush()
    return total


async def reindex_company(session: AsyncSession, embedder: Embedder, company_id: uuid.UUID, *,
                          force: bool = False) -> int:
    docs = (await session.scalars(select(Document).where(Document.company_id == company_id))).all()
    return await index_documents(session, embedder, docs, force=force)


@dataclass
class SearchHit:
    chunk_id: int
    document_id: str
    text: str
    title: str
    url: str
    source_type: str
    is_competitor: bool
    published_at: str | None
    score: float
    semantic_rank: int | None
    keyword_rank: int | None
    rerank_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def citation(self) -> str:
        return f"[{self.title or self.url}]({self.url})"


_HYBRID_SQL = text("""
WITH sem AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS rnk
    FROM chunks c
    WHERE c.company_id = :cid AND (CAST(:competitor AS boolean) IS NULL OR c.is_competitor = :competitor)
    ORDER BY c.embedding <=> CAST(:qvec AS vector)
    LIMIT :k
),
kw AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.query) DESC) AS rnk
    FROM chunks c, (SELECT to_tsquery('russian', :tsq) AS query) q
    WHERE :tsq <> '' AND c.company_id = :cid
      AND (CAST(:competitor AS boolean) IS NULL OR c.is_competitor = :competitor)
      AND c.tsv @@ q.query
    ORDER BY ts_rank_cd(c.tsv, q.query) DESC
    LIMIT :k
),
fused AS (
    SELECT COALESCE(sem.id, kw.id) AS id, sem.rnk AS sem_rnk, kw.rnk AS kw_rnk,
           COALESCE(1.0 / (:rrf_k + sem.rnk), 0) + COALESCE(1.0 / (:rrf_k + kw.rnk), 0) AS score
    FROM sem FULL OUTER JOIN kw ON sem.id = kw.id
)
SELECT f.id, f.sem_rnk, f.kw_rnk, f.score, c.text, c.is_competitor, d.id AS document_id, d.title, d.url,
       d.source_type, d.published_at
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
WHERE c.company_id = :cid AND d.company_id = :cid
ORDER BY f.score DESC
LIMIT :limit
""")

_WORD = re.compile(r"[\wё]{2,}", re.I | re.U)


def keyword_tsquery(query: str) -> str:
    """OR-запрос из слов (лексемы нормализует сам to_tsquery('russian', ...)); защищён от синтаксиса tsquery."""
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))[:20]
    return " | ".join(words)


class HybridSearcher:
    def __init__(self, embedder: Embedder, reranker: Any | None = None):
        self.embedder = embedder
        self.reranker = reranker  # опциональный локальный CrossEncoder

    async def search(self, session: AsyncSession, company_id: uuid.UUID, query: str, *, limit: int = 8,
                     competitor: bool | None = False, candidates: int = 40,
                     per_document: int | None = 2) -> list[SearchHit]:
        if not company_id:
            raise ValueError("company_id обязателен — поиск без компании запрещён")
        qvec = await self.embedder.embed_query(query)
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
        rows = (await session.execute(_HYBRID_SQL, {
            "qvec": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]",
            "cid": company_id, "competitor": competitor, "tsq": keyword_tsquery(query),
            "k": candidates, "rrf_k": RRF_K, "limit": candidates if (self.reranker or per_document) else limit,
        })).all()
        hits = [SearchHit(chunk_id=r.id, document_id=str(r.document_id), text=r.text, title=r.title, url=r.url,
                          source_type=r.source_type, is_competitor=r.is_competitor,
                          published_at=r.published_at.isoformat() if r.published_at else None,
                          score=float(r.score), semantic_rank=r.sem_rnk, keyword_rank=r.kw_rnk) for r in rows]
        if self.reranker and hits:
            scores = self.reranker.predict([(query, h.text) for h in hits])
            for h, s in zip(hits, scores):
                h.rerank_score = float(s)
            hits.sort(key=lambda h: h.rerank_score or 0, reverse=True)
        if per_document:  # не больше N фрагментов одного документа — разнообразие источников
            seen: dict[str, int] = {}
            kept = []
            for h in hits:
                seen[h.document_id] = seen.get(h.document_id, 0) + 1
                if seen[h.document_id] <= per_document:
                    kept.append(h)
            hits = kept
        return hits[:limit]


async def verify_quote(session: AsyncSession, company_id: uuid.UUID, document_id: uuid.UUID, quote: str) -> bool:
    """Проверка источника: встречается ли цитата (без учёта регистра/пробелов) в документе этой компании."""
    doc = await session.scalar(select(Document).where(Document.id == document_id, Document.company_id == company_id))
    if doc is None:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    return norm(quote) in norm(f"{doc.title}\n{doc.text}")
