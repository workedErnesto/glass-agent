"""Проверка материалов перед отправкой на согласование и публикацию (п. 3.3 ТЗ).

Правила без AI: лимиты площадок, пустой текст, незаполненные шаблоны, запрещённые
слова бренда, обязательные хештеги/слова, ссылки на чужие домены, повтор недавних
постов, наличие изображения. errors блокируют отправку, warnings показываются согласующему.
Правила бренда — settings.brand компании:
  {"banned_words": [...], "required_words": [...], "allowed_domains": ["salstek.ru"],
   "max_hashtags": 5, "require_image": false}
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.db.models import Company, Post

LIMITS = {"telegram": 4096, "vk": 15895, "max": 4000}
CAPTION_LIMIT = 1024
PLACEHOLDER = re.compile(r"\[(?:вставьте|название|ссылка|имя|дата|цена)[^\]]*\]|\{\{|\}\}|<[A-ZА-Я_ ]{3,}>|TODO|XXX|lorem ipsum",
                         re.I)
URL = re.compile(r"https?://[^\s)\]]+", re.I)
HASHTAG = re.compile(r"(?<![\w#])#[\wё]+", re.I)


def check_post(post: Post, company: Company, recent_texts: list[str] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    text = (post.text or "").strip()
    brand = (company.settings or {}).get("brand") or {}
    if not text:
        errors.append("Пустой текст")
    limit = LIMITS.get(post.platform, 4096)
    if len(text) > limit:
        errors.append(f"Текст {len(text)} символов — больше лимита {post.platform} ({limit})")
    has_image = bool(post.image_path and Path(post.image_path).is_file())
    if post.image_path and not has_image:
        errors.append("Файл изображения не найден")
    if post.platform == "telegram" and has_image and len(text) > CAPTION_LIMIT:
        warnings.append(f"Текст длиннее подписи к фото ({CAPTION_LIMIT}) — фото и текст уйдут двумя сообщениями")
    if brand.get("require_image") and not has_image:
        errors.append("По правилам бренда пост должен быть с изображением")
    if m := PLACEHOLDER.search(text):
        errors.append(f"Незаполненный шаблон: «{m.group(0)}»")
    low = text.lower()
    for w in brand.get("banned_words") or []:
        if w and w.lower() in low:
            errors.append(f"Запрещённое слово бренда: «{w}»")
    for w in brand.get("required_words") or []:
        if w and w.lower() not in low:
            warnings.append(f"Нет обязательного слова/хештега: «{w}»")
    allowed = [d.lower().lstrip(".") for d in brand.get("allowed_domains") or []]
    if allowed:
        for url in URL.findall(text):
            host = (urlsplit(url).hostname or "").lower()
            if not any(host == d or host.endswith("." + d) for d in allowed):
                warnings.append(f"Ссылка на сторонний домен: {host}")
    tags = HASHTAG.findall(text)
    if len(tags) > int(brand.get("max_hashtags", 5)):
        warnings.append(f"Много хештегов: {len(tags)}")
    for old in recent_texts or []:
        if old and difflib.SequenceMatcher(None, low[:1500], old.lower()[:1500]).ratio() > 0.85:
            warnings.append("Текст почти совпадает с недавней публикацией")
            break
    return {"ok": not errors, "errors": errors, "warnings": warnings}
