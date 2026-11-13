"""Разбиение документов на фрагменты для поиска."""
from __future__ import annotations

import re

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    out, cur = [], ""
    for sent in _SENT_SPLIT.split(paragraph):
        while len(sent) > max_chars:  # очень длинное «предложение» (таблица, список без точек)
            cut = sent.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if cur and len(cur) + 1 + len(sent) > max_chars:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, *, title: str = "", max_chars: int = 900, overlap_chars: int = 150) -> list[str]:
    """Чанки по абзацам ~max_chars с перекрытием; каждый чанк начинается с заголовка документа."""
    paragraphs: list[str] = []
    for p in re.split(r"\n\s*\n|\n(?=[-•*]|\|)", text or ""):
        p = p.strip()
        if p:
            paragraphs.extend(_split_long(p, max_chars))
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for p in paragraphs:
        if cur and size + len(p) > max_chars:
            chunks.append("\n".join(cur))
            tail = cur[-1] if len(cur[-1]) <= overlap_chars else ""
            cur, size = ([tail] if tail else []), len(tail)
        cur.append(p)
        size += len(p) + 1
    if cur:
        chunks.append("\n".join(cur))
    prefix = f"{title.strip()}\n" if title and title.strip() else ""
    result = []
    for c in chunks:
        if prefix and not c.startswith(title.strip()):
            c = prefix + c
        result.append(c)
    if not result and title:
        result = [title.strip()]
    return result
