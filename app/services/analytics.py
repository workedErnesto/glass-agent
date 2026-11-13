"""Аналитика только по реально собранным данным (никакой выдуманной статистики).

Показатели берутся из документов парсеров (Telegram/VK) и таблицы posts.
Если показателя нет в источнике, он возвращается как None с пояснением.
"""
from __future__ import annotations

import statistics
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Post, Source


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _engagement(m: dict[str, Any]) -> float | None:
    parts = [_num(m.get(k)) for k in ("reactions_total", "likes", "comments", "reposts", "forwards")]
    parts = [p for p in parts if p is not None]
    return sum(parts) if parts else None


def _doc_brief(d: Document) -> dict[str, Any]:
    return {"title": d.title[:120], "url": d.url, "published_at": d.published_at.isoformat() if d.published_at else None,
            "metrics": {k: v for k, v in (d.metrics or {}).items() if k != "reactions"}}


def channel_stats(docs: list[Document]) -> dict[str, Any]:
    if not docs:
        return {"posts": 0, "note": "Нет собранных постов по этому источнику"}
    views = [v for v in (_num((d.metrics or {}).get("views")) for d in docs) if v is not None]
    eng = [(d, e) for d in docs if (e := _engagement(d.metrics or {})) is not None]
    dates = sorted(d.published_at for d in docs if d.published_at)
    hashtags = Counter(h for d in docs for h in (d.meta or {}).get("hashtags", []))
    media = Counter(t for d in docs for t in (d.meta or {}).get("media_types", []))
    hours = Counter(d.published_at.astimezone(timezone.utc).hour for d in docs if d.published_at)
    per_week = None
    if len(dates) >= 2:
        span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1)
        per_week = round(len(dates) / span_days * 7, 2)
    approx = any((d.metrics or {}).get("approximate") for d in docs)
    formats = format_effectiveness(docs)
    by_views = sorted((d for d in docs if _num((d.metrics or {}).get("views")) is not None),
                      key=lambda d: d.metrics["views"], reverse=True)
    return {
        "posts": len(docs),
        "period": {"from": dates[0].isoformat(), "to": dates[-1].isoformat()} if dates else None,
        "posts_per_week": per_week,
        "views": {"posts_with_views": len(views), "avg": round(statistics.mean(views), 1) if views else None,
                  "median": statistics.median(views) if views else None, "max": max(views) if views else None},
        "engagement": {"posts_with_data": len(eng),
                       "avg": round(statistics.mean(e for _, e in eng), 2) if eng else None},
        "top_by_views": [_doc_brief(d) for d in by_views[:5]],
        "top_by_engagement": [_doc_brief(d) for d, _ in sorted(eng, key=lambda x: x[1], reverse=True)[:5]],
        "hashtags": hashtags.most_common(10),
        "media_types": media.most_common(),
        "posting_hours_utc": sorted(hours.items()),
        "format_effectiveness": formats,
        "notes": (["Просмотры и реакции в веб-превью Telegram округлены (например 24.5K)"] if approx else [])
                 + ([] if views else ["Источник не отдаёт просмотры"]),
    }


def _bucket_len(n: int) -> str:
    return "до 300" if n < 300 else "300–800" if n < 800 else "800–1500" if n < 1500 else "1500+"


def format_effectiveness(docs: list[Document]) -> dict[str, Any]:
    """Средние просмотры/вовлечённость по формату: тип медиа, длина текста, час публикации, день недели."""
    groups: dict[str, dict[str, list[float]]] = {"media": {}, "length": {}, "hour_utc": {}, "weekday": {}}
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    for d in docs:
        m = d.metrics or {}
        v = _num(m.get("views"))
        e = _engagement(m)
        score = v if v is not None else e
        if score is None:
            continue
        media = "+".join(sorted(set((d.meta or {}).get("media_types") or []))) or "текст"
        keys = {"media": media, "length": _bucket_len(len(d.text or ""))}
        if d.published_at:
            dt = d.published_at.astimezone(timezone.utc)
            keys["hour_utc"] = f"{dt.hour:02d}"
            keys["weekday"] = wd[dt.weekday()]
        for g, k in keys.items():
            groups[g].setdefault(k, []).append(score)
    metric = "views" if any(_num((d.metrics or {}).get("views")) is not None for d in docs) else "engagement"
    out: dict[str, Any] = {"metric": metric}
    for g, vals in groups.items():
        rows = [{"value": k, "posts": len(v), "avg": round(statistics.mean(v), 1)} for k, v in vals.items() if len(v) >= 2]
        out[g] = sorted(rows, key=lambda r: r["avg"], reverse=True)
    return out


def observations(stats: dict[str, Any]) -> list[str]:
    """Наблюдения, вычисленные из данных (не гипотезы модели)."""
    obs = []
    fe = stats.get("format_effectiveness") or {}
    label = "просмотров" if fe.get("metric") == "views" else "реакций/комментариев"
    for group, title in (("media", "Формат"), ("length", "Длина текста"), ("weekday", "День недели"),
                         ("hour_utc", "Час публикации (UTC)")):
        rows = fe.get(group) or []
        if len(rows) >= 2 and rows[-1]["avg"] > 0:
            best, worst = rows[0], rows[-1]
            obs.append(f"{title}: лучше всего «{best['value']}» — в среднем {best['avg']:g} {label} "
                       f"({best['posts']} постов), хуже всего «{worst['value']}» — {worst['avg']:g}")
    if stats.get("posts_per_week") is not None:
        obs.append(f"Частота: {stats['posts_per_week']:g} постов в неделю")
    return obs


async def source_docs(session: AsyncSession, company_id: uuid.UUID, platform: str, ref: str | None = None,
                      *, competitor: bool | None = None, limit: int = 2000) -> tuple[list[Source], list[Document]]:
    types = {"telegram": ("telegram", "telegram_web", "telegram_export", "telegram_bot"), "vk": ("vk",)}[platform]
    q = select(Source).where(Source.company_id == company_id, Source.type.in_(types))
    if competitor is not None:
        q = q.where(Source.is_competitor == competitor)
    sources = list((await session.scalars(q)).all())
    if ref:
        needle = ref.strip().lower().lstrip("@").rstrip("/").rsplit("/", 1)[-1]
        sources = [s for s in sources if needle and (needle in s.ref.lower() or needle == str(s.id))]
    if not sources:
        return [], []
    docs = (await session.scalars(select(Document).where(
        Document.company_id == company_id, Document.source_id.in_([s.id for s in sources]))
        .order_by(Document.published_at.desc().nulls_last()).limit(limit))).all()
    return sources, list(docs)


async def analyze_channel(session: AsyncSession, company_id: uuid.UUID, platform: str, ref: str | None) -> dict[str, Any]:
    sources, docs = await source_docs(session, company_id, platform, ref)
    if not sources:
        return {"error": "Источник не подключён к компании", "platform": platform, "ref": ref,
                "hint": "Добавьте источник и выполните синхронизацию"}
    return {"platform": platform, "sources": [{"id": str(s.id), "ref": s.ref, "is_competitor": s.is_competitor,
                                               "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None}
                                              for s in sources],
            **channel_stats(docs)}


async def post_analytics(session: AsyncSession, post: Post) -> dict[str, Any]:
    """Показатели опубликованного поста — из собранного парсером поста канала с тем же message_id."""
    base = {"post_id": str(post.id), "status": post.status, "external_message_id": post.external_message_id,
            "external_url": post.external_url}
    if post.status != "published" or not post.external_message_id:
        return {**base, "metrics": None, "note": "Пост ещё не опубликован — статистики нет"}
    docs = (await session.scalars(select(Document).where(
        Document.company_id == post.company_id, Document.source_type == "telegram"))).all()
    mid = int(post.external_message_id)
    match = next((d for d in docs if mid in ((d.meta or {}).get("message_ids") or [])
                  and (not post.target_chat_id or _same_chat(post.target_chat_id, d))), None)
    if match is None:
        return {**base, "metrics": None,
                "note": "Показатели ещё не собраны: синхронизируйте источник канала (telegram_web/telegram)"}
    return {**base, "metrics": match.metrics, "collected_at": match.fetched_at.isoformat() if match.fetched_at else None}


def _same_chat(target: str, d: Document) -> bool:
    meta = d.meta or {}
    t = target.lstrip("@").lower()
    return t in {str(meta.get("channel_username") or "").lower(), str(meta.get("channel_id") or ""),
                 d.external_key.split(":")[1].lower()}


async def company_analytics(session: AsyncSession, company_id: uuid.UUID) -> dict[str, Any]:
    posts_by_status = dict((await session.execute(select(Post.status, func.count()).where(
        Post.company_id == company_id).group_by(Post.status))).all())
    docs_by_type = dict((await session.execute(select(Document.source_type, func.count()).where(
        Document.company_id == company_id).group_by(Document.source_type))).all())
    channels = {}
    for platform in ("telegram", "vk"):
        for competitor in (False, True):
            _, docs = await source_docs(session, company_id, platform, competitor=competitor)
            if docs:
                channels[f"{platform}{'_competitors' if competitor else ''}"] = channel_stats(docs)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "posts_by_status": posts_by_status,
            "knowledge_documents": docs_by_type, "channels": channels}


async def competitors_comparison(session: AsyncSession, company_id: uuid.UUID) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for platform in ("telegram", "vk"):
        own_src, own = await source_docs(session, company_id, platform, competitor=False)
        comp_src, comp = await source_docs(session, company_id, platform, competitor=True)
        if not own and not comp:
            continue
        per_comp = {}
        for s in comp_src:
            docs = [d for d in comp if d.source_id == s.id]
            per_comp[s.name or s.ref] = channel_stats(docs)
        out[platform] = {"own": channel_stats(own) if own else None, "competitors": per_comp}
    if not out:
        return {"note": "Нет данных: подключите свои каналы и каналы конкурентов (is_competitor=true) и синхронизируйте"}
    return out


async def period_report(session: AsyncSession, company_id: uuid.UUID, *, days: int = 7) -> dict[str, Any]:
    """Отчёт за период по собранным данным: показатели, форматы, публикации системы, наблюдения."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    channels = {}
    for platform in ("telegram", "vk"):
        _, docs = await source_docs(session, company_id, platform, competitor=False)
        period_docs = [d for d in docs if d.published_at and d.published_at >= since]
        if docs:
            all_stats = channel_stats(docs)
            channels[platform] = {"period": channel_stats(period_docs) if period_docs else {"posts": 0},
                                  "all_time_avg_views": all_stats["views"]["avg"],
                                  "observations": observations(all_stats)}
    published = (await session.scalars(select(Post).where(Post.company_id == company_id, Post.status == "published",
                                                          Post.published_at >= since))).all()
    pub = []
    for p in published:
        a = await post_analytics(session, p)
        pub.append({"id": str(p.id), "topic": p.topic, "platform": p.platform, "url": p.external_url,
                    "metrics": a.get("metrics")})
    posts_by_status = dict((await session.execute(select(Post.status, func.count()).where(
        Post.company_id == company_id).group_by(Post.status))).all())
    return {"days": days, "since": since.isoformat(), "channels": channels, "published_by_system": pub,
            "posts_by_status": posts_by_status}


def report_text(company_name: str, rep: dict[str, Any], conclusions: str | None = None) -> str:
    lines = [f"📊 Отчёт «{company_name}» за {rep['days']} дн."]
    for platform, ch in rep["channels"].items():
        per = ch["period"]
        views = (per.get("views") or {}).get("avg")
        lines.append(f"\n{platform}: постов за период {per.get('posts', 0)}"
                     + (f", средние просмотры {views:g} (за всё время {ch['all_time_avg_views']:g})"
                        if views is not None and ch.get("all_time_avg_views") is not None else ""))
        for top in (per.get("top_by_views") or [])[:3]:
            lines.append(f"  • {top['title'][:60]} — {top['metrics'].get('views')} просм. {top['url']}")
        for o in ch.get("observations", [])[:4]:
            lines.append(f"  – {o}")
    if rep["published_by_system"]:
        lines.append("\nОпубликовано системой:")
        for p in rep["published_by_system"]:
            m = p["metrics"] or {}
            lines.append(f"  • {p['topic'] or p['id']} ({p['platform']}) — "
                         + (f"{m.get('views')} просм." if m.get("views") is not None else "показатели ещё не собраны"))
    if not rep["channels"] and not rep["published_by_system"]:
        lines.append("Нет собранных данных: подключите и синхронизируйте каналы.")
    if conclusions:
        lines.append(f"\nВыводы и рекомендации (AI):\n{conclusions}")
    else:
        lines.append("\nВыводы и рекомендации AI недоступны (модель не подключена) — выше только показатели из данных.")
    return "\n".join(lines)
