"""Локальные эмбеддинги (sentence-transformers). Без OpenAI Embeddings API.

intfloat/multilingual-e5-* требует префиксы: "query: " для запросов,
"passage: " для документов. Векторы нормализованы → косинусная близость.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from functools import lru_cache
from typing import Protocol

log = logging.getLogger(__name__)


class Embedder(Protocol):
    dim: int

    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


class E5Embedder:
    def __init__(self, model_name: str = "intfloat/multilingual-e5-small", device: str | None = None,
                 batch_size: int = 32):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()
        self.dim = 384

    def _load(self):
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                device = self.device
                if device is None:
                    import torch
                    device = "mps" if torch.backends.mps.is_available() else "cpu"
                log.info("Загрузка модели эмбеддингов %s на %s", self.model_name, device)
                self._model = SentenceTransformer(self.model_name, device=device)
                self.dim = self._model.get_sentence_embedding_dimension()
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                            show_progress_bar=False, convert_to_numpy=True)
        return [v.tolist() for v in vecs]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._encode, [f"passage: {t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await asyncio.to_thread(self._encode, [f"query: {text}"]))[0]


@lru_cache
def get_embedder() -> E5Embedder:
    from app.core.config import get_settings

    return E5Embedder(get_settings().embedding_model)
