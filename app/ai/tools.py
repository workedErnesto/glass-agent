"""Инструменты агента.

Компания задаётся сервером в ToolContext (из авторизованного запроса), а не
аргументом модели: ни один инструмент не принимает company_id, поэтому модель
не может обратиться к данным другой компании.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, Document, Source
from app.rag.index import HybridSearcher
from app.services import analytics, posts as posts_svc
from app.services import memory as mem

from .connectors.base import AIConnector, AIMessage, AIResult


class ToolError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    """Ограничение расхода лимитов ChatGPT Plus и времени на один запрос пользователя."""
    max_llm_calls: int = 8
    max_seconds: float = 240
    llm_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def check(self) -> None:
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded(f"Исчерпан лимит вызовов модели на запрос ({self.max_llm_calls})")
        if time.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded(f"Превышено время выполнения ({int(self.max_seconds)} с)")

    @property
    def remaining_calls(self) -> int:
        return self.max_llm_calls - self.llm_calls


@dataclass
class ToolContext:
    session: AsyncSession
    company_id: uuid.UUID
    connector: AIConnector
    searcher: HybridSearcher
    user_id: int | None = None
    budget: Budget = field(default_factory=Budget)
    last_result: AIResult | None = None
    created_post_ids: list[str] = field(default_factory=list)
    notifier: Any = None
    # действия после фиксации транзакции (уведомления), вызываются оркестратором: fn(session) -> awaitable
    after_commit: list[Callable[[AsyncSession], Awaitable[Any]]] = field(default_factory=list)

    async def company(self) -> Company:
        c = await self.session.get(Company, self.company_id)
        if c is None:
            raise ToolError("Компания не найдена")
        return c

    async def complete(self, messages: list[AIMessage], *, instructions: str | None = None) -> AIResult:
        """Единая точка вызова модели: лимит запроса, дневной лимит компании, учёт расхода."""
        from app.services import usage

        self.budget.check()
        await usage.check_quota(self.session, self.company_id)
        self.budget.llm_calls += 1
        res = await self.connector.complete(messages, instructions=instructions)
        self.last_result = res
        await usage.record(self.session, self.company_id, res)
        return res

    async def llm(self, prompt: str, instructions: str) -> AIResult:
        return await self.complete([AIMessage("user", prompt)], instructions=instructions)

    @property
    def metered(self) -> "MeteredConnector":
        return MeteredConnector(self)

    def ai_meta(self) -> dict[str, Any]:
        r = self.last_result
        return {"provider": r.provider, "model": r.model, "is_mock": r.is_mock} if r else {}


class MeteredConnector:
    """Коннектор поверх ToolContext — для вспомогательных вызовов (сводки), которые тоже учитываются."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.name = getattr(ctx.connector, "name", "")

    async def complete(self, messages, *, instructions=None, model=None) -> AIResult:
        return await self.ctx.complete(messages, instructions=instructions)


ToolFn = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]
    fn: ToolFn
    uses_llm: bool = False


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict[str, str] | None = None, *, uses_llm: bool = False):
    def deco(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = Tool(name, description, params or {}, fn, uses_llm)
        return fn
    return deco


def catalog() -> str:
    lines = []
    for t in REGISTRY.values():
        args = ", ".join(f"{k}: {v}" for k, v in t.params.items())
        lines.append(f"- {t.name}({args}) — {t.description}")
    return "\n".join(lines)


async def call_tool(ctx: ToolContext, name: str, args: dict[str, Any] | None) -> Any:
    t = REGISTRY.get(name)
    if t is None:
        raise ToolError(f"Неизвестный инструмент: {name}")
    args = {k: v for k, v in (args or {}).items() if k in t.params}  # лишние аргументы (в т.ч. company_id) отбрасываются
    return await t.fn(ctx, **args)


# --------------------------------------------------------------------------- знания

def _hits(hits) -> list[dict[str, Any]]:
    return [{"n": i, "title": h.title, "url": h.url, "source_type": h.source_type, "is_competitor": h.is_competitor,
             "published_at": h.published_at, "text": h.text[:900]} for i, h in enumerate(hits, 1)]


@tool("search_company_knowledge", "поиск по базе знаний компании (сайт, посты, файлы); возвращает фрагменты со ссылками",
      {"query": "строка запроса"})
async def search_company_knowledge(ctx: ToolContext, query: str = "") -> Any:
    if not query.strip():
        raise ToolError("Пустой запрос")
    return _hits(await ctx.searcher.search(ctx.session, ctx.company_id, query, limit=6, competitor=False))


@tool("search_competitors", "поиск только по данным конкурентов компании", {"query": "строка запроса"})
async def search_competitors(ctx: ToolContext, query: str = "") -> Any:
    if not query.strip():
        raise ToolError("Пустой запрос")
    hits = await ctx.searcher.search(ctx.session, ctx.company_id, query, limit=6, competitor=True)
    return _hits(hits) or {"note": "Данных конкурентов нет: добавьте источник с флагом конкурента"}


@tool("get_company_profile", "профиль компании и подключённые источники")
async def get_company_profile(ctx: ToolContext) -> Any:
    c = await ctx.company()
    sources = (await ctx.session.scalars(select(Source).where(Source.company_id == c.id))).all()
    return {"name": c.name, "slug": c.slug, "profile": c.profile,
            "sources": [{"id": str(s.id), "type": s.type, "ref": s.ref, "is_competitor": s.is_competitor,
                         "status": s.status} for s in sources]}


@tool("analyze_telegram_channel", "статистика подключённого Telegram-канала по собранным постам",
      {"channel_id": "имя канала, ссылка или id источника; пусто — все каналы компании"})
async def analyze_telegram_channel(ctx: ToolContext, channel_id: str = "") -> Any:
    return await analytics.analyze_channel(ctx.session, ctx.company_id, "telegram", channel_id or None)


@tool("analyze_vk_group", "статистика подключённого сообщества VK по собранным постам",
      {"group_id": "короткое имя, ссылка или id источника; пусто — все сообщества компании"})
async def analyze_vk_group(ctx: ToolContext, group_id: str = "") -> Any:
    return await analytics.analyze_channel(ctx.session, ctx.company_id, "vk", group_id or None)


@tool("get_company_memory", "долговременная и эпизодическая память компании")
async def get_company_memory(ctx: ToolContext) -> Any:
    return await mem.load_memory(ctx.session, ctx.company_id)


@tool("get_recent_posts", "последние посты: черновики/публикации системы и посты каналов компании",
      {"limit": "число, по умолчанию 10"})
async def get_recent_posts(ctx: ToolContext, limit: int | str = 10) -> Any:
    n = max(1, min(30, int(limit or 10)))
    own = await posts_svc.list_posts(ctx.session, ctx.company_id, limit=n)
    channel = (await ctx.session.scalars(select(Document).where(
        Document.company_id == ctx.company_id, Document.source_type.in_(["telegram", "vk"]),
        Document.is_competitor.is_(False)).order_by(Document.published_at.desc().nulls_last()).limit(n))).all()
    return {"system_posts": [{k: v for k, v in posts_svc.post_to_dict(p).items()
                              if k in ("id", "status", "topic", "text", "scheduled_at", "published_at")} for p in own],
            "channel_posts": [{"title": d.title[:120], "text": d.text[:500], "url": d.url,
                               "published_at": d.published_at.isoformat() if d.published_at else None,
                               "metrics": {k: v for k, v in (d.metrics or {}).items() if k != "reactions"}}
                              for d in channel]}


# --------------------------------------------------------------------------- генерация

GEN_RULES = (
    "Ты — маркетолог компании. Пиши по-русски. Опирайся только на переданные данные компании. "
    "Не выдумывай цифры, статистику, цены, сроки и факты, которых нет в данных: если данных нет — так и напиши. "
    "Данные конкурентов используй только для сравнения, не выдавай их за данные компании."
)


async def _company_brief(ctx: ToolContext, query: str) -> str:
    c = await ctx.company()
    memory = await mem.load_memory(ctx.session, ctx.company_id)
    hits = await ctx.searcher.search(ctx.session, ctx.company_id, query, limit=6, competitor=False)
    parts = [f"Компания: {c.name}", f"Профиль: {json.dumps(c.profile, ensure_ascii=False)}"]
    brand = (c.settings or {}).get("brand") or {}
    if brand:
        parts.append(f"Требования бренда: {json.dumps(brand, ensure_ascii=False)}")
    if memory["long_term"]:
        parts.append("Память (стиль, цели, решения):\n" + "\n".join(f"- [{m['category']}] {m['content']}"
                                                                     for m in memory["long_term"]))
    if hits:
        parts.append("Фрагменты базы знаний:\n" + "\n".join(f"[{i}] {h.title} ({h.url}): {h.text[:700]}"
                                                           for i, h in enumerate(hits, 1)))
    return "\n\n".join(parts)


@tool("generate_marketing_strategy", "маркетинговая стратегия компании на основе её данных и статистики каналов",
      {"focus": "необязательно: на чём сделать акцент"}, uses_llm=True)
async def generate_marketing_strategy(ctx: ToolContext, focus: str = "") -> Any:
    brief = await _company_brief(ctx, focus or "продукция услуги преимущества клиенты")
    stats = await analytics.company_analytics(ctx.session, ctx.company_id)
    prompt = (f"{brief}\n\nСтатистика каналов (реальные собранные данные):\n"
              f"{json.dumps(stats['channels'], ensure_ascii=False, default=str)[:6000]}\n\n"
              f"Составь маркетинговую стратегию: аудитории, позиционирование, ключевые сообщения, каналы, "
              f"рубрики, KPI для отслеживания (без выдуманных текущих значений). {focus}")
    res = await ctx.llm(prompt, GEN_RULES)
    await mem.remember(ctx.session, ctx.company_id, f"Подготовлена маркетинговая стратегия{': ' + focus if focus else ''}",
                       kind="episodic", category="strategy", meta={"text": res.text[:3000], **ctx.ai_meta()})
    return {"strategy": res.text}


@tool("generate_content_plan", "контент-план на период", {"period": "например «неделя», «ноябрь», «2 недели»"},
      uses_llm=True)
async def generate_content_plan(ctx: ToolContext, period: str = "неделя") -> Any:
    brief = await _company_brief(ctx, "продукция новости проекты акции")
    recent = await get_recent_posts(ctx, 15)
    prompt = (f"{brief}\n\nНедавние посты каналов:\n"
              f"{json.dumps(recent['channel_posts'], ensure_ascii=False, default=str)[:4000]}\n\n"
              f"Составь контент-план на период «{period}»: дата/день, площадка, рубрика, тема, формат, ключевой тезис. "
              "Не повторяй темы недавних постов.")
    res = await ctx.llm(prompt, GEN_RULES)
    await mem.remember(ctx.session, ctx.company_id, f"Подготовлен контент-план на период «{period}»",
                       kind="episodic", category="content_plan", meta={"text": res.text[:3000], **ctx.ai_meta()})
    return {"period": period, "plan": res.text}


PLATFORM_STYLE = {
    "telegram": "Telegram-канала (до 900 символов, абзацы, 1–3 эмодзи, 1–3 хештега, без markdown)",
    "vk": "сообщества ВКонтакте (до 1500 символов, живой тон, вопрос к аудитории в конце, 2–5 хештегов)",
    "max": "канала MAX (до 900 символов, коротко и по делу, без markdown, 0–2 хештега)",
}


def post_rules(platform: str) -> str:
    return GEN_RULES + (f" Верни только текст поста для {PLATFORM_STYLE.get(platform, PLATFORM_STYLE['telegram'])}, "
                        "без пояснений и вариантов. Не оставляй шаблонов вида [вставьте …].")


POST_RULES = post_rules("telegram")


def _platform(value: str) -> str:
    value = (value or "telegram").strip().lower()
    aliases = {"tg": "telegram", "телеграм": "telegram", "вк": "vk", "вконтакте": "vk", "vkontakte": "vk", "макс": "max"}
    value = aliases.get(value, value)
    if value not in posts_svc.PLATFORMS:
        raise ToolError(f"Площадка {value} не поддерживается: {', '.join(posts_svc.PLATFORMS)}")
    return value


def _parse_time(value: str | None):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError("Время в формате ISO 8601, например 2026-11-20T10:00:00+03:00") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@tool("create_post_draft", "создать черновик поста по теме для площадки (сохраняется со статусом draft)",
      {"topic": "тема поста", "platform": "telegram | vk | max, по умолчанию telegram"}, uses_llm=True)
async def create_post_draft(ctx: ToolContext, topic: str = "", platform: str = "telegram") -> Any:
    if not topic.strip():
        raise ToolError("Не указана тема")
    platform = _platform(platform)
    brief = await _company_brief(ctx, topic)
    res = await ctx.llm(f"{brief}\n\nНапиши пост на тему: {topic}", post_rules(platform))
    post = await posts_svc.create_draft(ctx.session, ctx.company_id, text=res.text, topic=topic, platform=platform,
                                        user_id=ctx.user_id, generated_by=ctx.ai_meta())
    ctx.created_post_ids.append(str(post.id))
    return {"post_id": str(post.id), "status": post.status, "platform": platform, "text": post.text}


@tool("update_post_draft", "переписать пост по инструкции или комментарию согласующего; потом нужно повторное согласование",
      {"post_id": "id поста", "instructions": "что изменить"}, uses_llm=True)
async def update_post_draft(ctx: ToolContext, post_id: str = "", instructions: str = "") -> Any:
    try:
        post = await posts_svc.get_post(ctx.session, ctx.company_id, post_id)
    except LookupError as e:
        raise ToolError(str(e)) from e
    if post.status not in posts_svc.EDITABLE:
        raise ToolError(f"Пост в статусе {post.status} изменять нельзя")
    instructions = instructions or post.review_comment or ""
    if not instructions:
        raise ToolError("Не указано, что изменить")
    brief = await _company_brief(ctx, post.topic or instructions)
    res = await ctx.llm(f"{brief}\n\nТекущий текст поста:\n{post.text}\n\nИзмени его: {instructions}",
                        post_rules(post.platform))
    await posts_svc.edit_content(ctx.session, post, text=res.text, user_id=ctx.user_id)
    post.generated_by = ctx.ai_meta()
    return {"post_id": str(post.id), "status": post.status, "revision": post.revision, "text": post.text}


@tool("adapt_post", "адаптировать пост под другую площадку — создаётся новый черновик",
      {"post_id": "id исходного поста", "platform": "telegram | vk | max"}, uses_llm=True)
async def adapt_post(ctx: ToolContext, post_id: str = "", platform: str = "") -> Any:
    platform = _platform(platform)
    try:
        src = await posts_svc.get_post(ctx.session, ctx.company_id, post_id)
    except LookupError as e:
        raise ToolError(str(e)) from e
    res = await ctx.llm(f"Исходный пост для {src.platform}:\n{src.text}\n\nАдаптируй его под площадку {platform}, "
                        "сохранив факты и смысл.", post_rules(platform))
    post = await posts_svc.create_draft(ctx.session, ctx.company_id, text=res.text, topic=src.topic, platform=platform,
                                        user_id=ctx.user_id, generated_by=ctx.ai_meta())
    if src.image_path:
        post.image_path, post.image_meta = src.image_path, dict(src.image_meta or {})
    post.meta = {**(post.meta or {}), "adapted_from": str(src.id)}
    ctx.created_post_ids.append(str(post.id))
    return {"post_id": str(post.id), "platform": platform, "text": post.text}


@tool("submit_for_approval", "проверить пост и отправить ответственному на согласование в Telegram",
      {"post_id": "id поста", "proposed_time": "необязательно: предлагаемое время публикации ISO 8601"})
async def submit_for_approval(ctx: ToolContext, post_id: str = "", proposed_time: str = "") -> Any:
    try:
        post = await posts_svc.get_post(ctx.session, ctx.company_id, post_id)
        await posts_svc.submit(ctx.session, post, user_id=ctx.user_id, proposed_at=_parse_time(proposed_time))
    except LookupError as e:
        raise ToolError(str(e)) from e
    except posts_svc.PostStateError as e:
        raise ToolError(str(e)) from e
    if ctx.notifier is not None and hasattr(ctx.notifier, "notify_approval"):
        ctx.after_commit.append(lambda s, p=post: ctx.notifier.notify_approval(s, p))
    return {"post_id": str(post.id), "status": post.status, "checks": (post.meta or {}).get("checks"),
            "proposed_at": post.proposed_at.isoformat() if post.proposed_at else None}


@tool("create_video_script", "сценарий короткого вертикального видео (Reels/Клипы/Shorts) по теме",
      {"topic": "тема", "duration": "длительность в секундах, по умолчанию 30"}, uses_llm=True)
async def create_video_script(ctx: ToolContext, topic: str = "", duration: int | str = 30) -> Any:
    if not topic.strip():
        raise ToolError("Не указана тема")
    sec = max(10, min(180, int(duration or 30)))
    brief = await _company_brief(ctx, topic)
    res = await ctx.llm(f"{brief}\n\nНапиши сценарий вертикального видео на {sec} секунд на тему «{topic}»: "
                        "хук в первые 3 секунды, раскадровка по сценам (время, что в кадре, текст на экране, "
                        "закадровый текст), призыв к действию, подпись к публикации и список нужных съёмок.",
                        GEN_RULES)
    await mem.remember(ctx.session, ctx.company_id, f"Подготовлен сценарий видео «{topic}» ({sec} с)",
                       kind="episodic", category="action", meta={"text": res.text[:3000], **ctx.ai_meta()})
    return {"topic": topic, "duration": sec, "script": res.text}


@tool("suggest_topics", "предложить темы публикаций с учётом знаний компании, прошлых постов и статистики",
      {"count": "сколько тем, по умолчанию 5"}, uses_llm=True)
async def suggest_topics(ctx: ToolContext, count: int | str = 5) -> Any:
    n = max(1, min(15, int(count or 5)))
    brief = await _company_brief(ctx, "продукция преимущества новинки проекты")
    stats = await analytics.company_analytics(ctx.session, ctx.company_id)
    recent = await get_recent_posts(ctx, 15)
    res = await ctx.llm(f"{brief}\n\nЧто лучше заходило (реальные данные):\n"
                        f"{json.dumps(stats['channels'], ensure_ascii=False, default=str)[:3000]}\n\n"
                        f"Недавние посты:\n{json.dumps([p['title'] for p in recent['channel_posts']], ensure_ascii=False)}"
                        f"\n\nПредложи {n} новых тем: тема, формат, почему сработает (со ссылкой на данные).", GEN_RULES)
    return {"topics": res.text}


@tool("analyze_competitors", "сравнение активности компании и конкурентов по собранным данным (без выдуманных цифр)")
async def analyze_competitors(ctx: ToolContext) -> Any:
    return await analytics.competitors_comparison(ctx.session, ctx.company_id)


@tool("build_report", "отчёт по маркетингу за период: показатели, лучшие форматы, наблюдения",
      {"days": "период в днях, по умолчанию 7"})
async def build_report(ctx: ToolContext, days: int | str = 7) -> Any:
    return await analytics.period_report(ctx.session, ctx.company_id, days=max(1, min(365, int(days or 7))))


@tool("prepare_post_image", "собрать картинку к посту из фирменного шаблона (Pillow, не AI-генерация)",
      {"post_id": "id поста", "title": "крупный текст на картинке (до 60 символов)",
       "subtitle": "необязательно: второй текст", "background": "необязательно: файл из медиатеки компании"})
async def prepare_post_image(ctx: ToolContext, post_id: str = "", title: str = "", subtitle: str = "",
                             background: str = "") -> Any:
    from app.core.config import get_settings
    from app.services import images

    try:
        post = await posts_svc.get_post(ctx.session, ctx.company_id, post_id)
    except LookupError as e:
        raise ToolError(str(e)) from e
    c = await ctx.company()
    s = get_settings()
    try:
        r = images.render_for_company(s.data_dir, c.slug, c.settings or {}, f"{post.id}-r{post.revision + 1}",
                                      title=title or post.topic or post.text[:60], subtitle=subtitle,
                                      background=background or None)
    except images.ImageError as e:
        raise ToolError(str(e)) from e
    await posts_svc.edit_content(ctx.session, post, image_path=str(r.path), image_meta=r.meta, user_id=ctx.user_id)
    return {"post_id": str(post.id), "image": r.path.name, "image_meta": r.meta, "status": post.status,
            "media_library": images.list_media(s.data_dir, c.slug)}


@tool("get_post_analytics", "показатели опубликованного поста (только реально собранные)", {"post_id": "id поста"})
async def get_post_analytics(ctx: ToolContext, post_id: str = "") -> Any:
    try:
        post = await posts_svc.get_post(ctx.session, ctx.company_id, post_id)
    except LookupError as e:
        raise ToolError(str(e)) from e
    return await analytics.post_analytics(ctx.session, post)
