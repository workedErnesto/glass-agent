"""Регулярные задачи компаний — работа без участия пользователя (п. 3.1 и 6 ТЗ).

Виды (kind):
  report            — отчёт за период (показатели из данных + выводы AI, если модель подключена) → Telegram
  sync_sources      — повторная синхронизация источников (сайт, каналы, конкуренты) с индексацией RAG
  collect_results   — показатели опубликованных постов → эпизодическая память
  collect_comments  — сбор комментариев (VK) во входящие; черновики/автоответы — по разрешениям
  agent             — поручение агенту текстом (params.prompt), ответ — в Telegram и в историю чата

Расписание: {"every_minutes": 360} или {"daily_at": "09:00", "weekdays": [0,3], "tz": "Europe/Moscow"}
(weekdays: 0 = понедельник; пусто = каждый день).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.connectors.base import AIError
from app.db.models import AuditLog, Company, Post, ScheduledTask, Source

from . import analytics
from . import memory as mem

log = logging.getLogger(__name__)
KINDS = ("report", "sync_sources", "collect_results", "collect_comments", "agent")
DEFAULT_TZ = "Europe/Moscow"
STUCK_AFTER = timedelta(minutes=30)


class TaskConfigError(ValueError):
    pass


def validate_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    if "every_minutes" in schedule:
        n = int(schedule["every_minutes"])
        if n < 5:
            raise TaskConfigError("every_minutes не меньше 5")
        return {"every_minutes": n}
    if "daily_at" in schedule:
        try:
            hh, mm = (int(x) for x in str(schedule["daily_at"]).split(":"))
            time(hh, mm)
        except (ValueError, TypeError) as e:
            raise TaskConfigError("daily_at в формате ЧЧ:ММ") from e
        wd = [int(x) for x in schedule.get("weekdays") or []]
        if any(x < 0 or x > 6 for x in wd):
            raise TaskConfigError("weekdays: 0..6 (0 — понедельник)")
        tz = schedule.get("tz") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except Exception as e:  # noqa: BLE001
            raise TaskConfigError(f"Неизвестный часовой пояс {tz}") from e
        return {"daily_at": f"{hh:02d}:{mm:02d}", "weekdays": sorted(set(wd)), "tz": tz}
    raise TaskConfigError("Нужно every_minutes или daily_at")


def next_run(schedule: dict[str, Any], after: datetime) -> datetime:
    schedule = validate_schedule(schedule)
    if "every_minutes" in schedule:
        return after + timedelta(minutes=schedule["every_minutes"])
    tz = ZoneInfo(schedule["tz"])
    hh, mm = (int(x) for x in schedule["daily_at"].split(":"))
    local = after.astimezone(tz)
    for add in range(0, 8):
        day = (local + timedelta(days=add)).date()
        cand = datetime.combine(day, time(hh, mm), tzinfo=tz)
        if cand > local and (not schedule["weekdays"] or cand.weekday() in schedule["weekdays"]):
            return cand.astimezone(timezone.utc)
    raise TaskConfigError("Не удалось вычислить следующий запуск")


DEFAULT_TASKS = [
    {"kind": "report", "title": "Еженедельный отчёт", "params": {"days": 7},
     "schedule": {"daily_at": "09:00", "weekdays": [0], "tz": DEFAULT_TZ}, "enabled": True},
    {"kind": "sync_sources", "title": "Обновление знаний и статистики", "params": {},
     "schedule": {"daily_at": "04:00", "tz": DEFAULT_TZ}, "enabled": True},
    {"kind": "collect_results", "title": "Сбор результатов публикаций", "params": {"days": 14},
     "schedule": {"every_minutes": 360}, "enabled": True, "notify": False},
    {"kind": "collect_comments", "title": "Сбор комментариев", "params": {},
     "schedule": {"every_minutes": 60}, "enabled": True, "notify": False},
    {"kind": "agent", "title": "Контент-план на неделю", "params": {
        "prompt": "Составь контент-план на следующую неделю с учётом статистики каналов и прошлых публикаций. "
                  "Подготовь черновик первого поста с картинкой и отправь его на согласование."},
     "schedule": {"daily_at": "10:00", "weekdays": [4], "tz": DEFAULT_TZ}, "enabled": False},
]


async def create_task(session: AsyncSession, company_id: uuid.UUID, *, kind: str, schedule: dict[str, Any],
                      title: str = "", params: dict[str, Any] | None = None, enabled: bool = True,
                      notify: bool = True, user_id: int | None = None) -> ScheduledTask:
    if kind not in KINDS:
        raise TaskConfigError(f"kind: {', '.join(KINDS)}")
    if kind == "agent" and not (params or {}).get("prompt"):
        raise TaskConfigError("Для agent нужен params.prompt")
    schedule = validate_schedule(schedule)
    t = ScheduledTask(company_id=company_id, kind=kind, title=title or kind, params=params or {}, schedule=schedule,
                      enabled=enabled, notify=notify, created_by_user_id=user_id, last_result={},
                      next_run_at=next_run(schedule, datetime.now(timezone.utc)))
    session.add(t)
    await session.flush()
    session.add(AuditLog(company_id=company_id, user_id=user_id, action="task.create",
                         details={"task_id": str(t.id), "kind": kind}))
    return t


async def create_default_tasks(session: AsyncSession, company_id: uuid.UUID) -> list[ScheduledTask]:
    return [await create_task(session, company_id, **{"notify": True, **spec}) for spec in DEFAULT_TASKS]


def task_dict(t: ScheduledTask) -> dict[str, Any]:
    return {"id": str(t.id), "kind": t.kind, "title": t.title, "params": t.params, "schedule": t.schedule,
            "enabled": t.enabled, "notify": t.notify, "run_count": t.run_count,
            "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
            "last_status": t.last_status, "last_result": t.last_result,
            "running": t.running_since is not None}


# --------------------------------------------------------------------------- выполнение

@dataclass
class TaskDeps:
    notifier: Any
    embedder: Any = None
    orchestrator: Any = None  # app.ai.orchestrator.Orchestrator
    publisher: Any = None  # для автоответов на комментарии


async def _ai_conclusions(session: AsyncSession, deps: TaskDeps, company_id: uuid.UUID,
                          rep: dict[str, Any]) -> str | None:
    if deps.orchestrator is None:
        return None
    import json

    from app.ai.tools import Budget, GEN_RULES, ToolContext

    ctx = ToolContext(session=session, company_id=company_id, connector=deps.orchestrator.connector,
                      searcher=deps.orchestrator.searcher, budget=Budget(max_llm_calls=1))
    try:
        res = await ctx.llm("Данные отчёта (реальные показатели):\n" + json.dumps(rep, ensure_ascii=False, default=str)[:8000]
                            + "\n\nСделай 3–5 выводов и 3 конкретных рекомендуемых действия с гипотезами для проверки. "
                              "Ссылайся только на показатели из данных.", GEN_RULES)
    except AIError as e:
        log.info("Выводы AI для отчёта недоступны: %s", e.code)
        return None
    return ("[ТЕСТОВАЯ МОДЕЛЬ] " if res.is_mock else "") + res.text


async def _run_report(sm, task: ScheduledTask, deps: TaskDeps) -> tuple[dict[str, Any], str]:
    async with sm() as s:
        company = await s.get(Company, task.company_id)
        rep = await analytics.period_report(s, task.company_id, days=int(task.params.get("days", 7)))
        conclusions = await _ai_conclusions(s, deps, task.company_id, rep)
        text = analytics.report_text(company.name, rep, conclusions)
        await mem.remember(s, task.company_id, f"Отправлен отчёт за {rep['days']} дн.", kind="episodic",
                           category="analysis", meta={"text": text[:3000]})
        await s.commit()
    return {"report": rep, "ai_conclusions": conclusions is not None}, text


async def _run_sync(sm, task: ScheduledTask, deps: TaskDeps) -> tuple[dict[str, Any], str]:
    from .sources import sync_source

    types = task.params.get("types")
    async with sm() as s:
        q = select(Source).where(Source.company_id == task.company_id, Source.status != "syncing")
        if types:
            q = q.where(Source.type.in_(types))
        sources = (await s.scalars(q)).all()
    out, lines = {}, []
    for src in sources:
        try:
            r = await sync_source(sm, src.id, deps.embedder, company_id=task.company_id)
            out[str(src.id)] = {"new": r.new, "updated": r.updated, "errors": len(r.errors)}
            lines.append(f"• {src.type} {src.name or src.ref}: новых {r.new}, обновлено {r.updated}"
                         + (f", ошибок {len(r.errors)}" if r.errors else ""))
        except Exception as e:  # noqa: BLE001 — один источник не останавливает остальные
            out[str(src.id)] = {"error": f"{type(e).__name__}: {e}"}
            lines.append(f"• {src.type} {src.name or src.ref}: ошибка {type(e).__name__}")
    return {"sources": out}, "🔄 Обновление источников:\n" + ("\n".join(lines) or "источников нет")


async def _run_collect_results(sm, task: ScheduledTask, deps: TaskDeps) -> tuple[dict[str, Any], str]:
    since = datetime.now(timezone.utc) - timedelta(days=int(task.params.get("days", 14)))
    found = 0
    async with sm() as s:
        posts = (await s.scalars(select(Post).where(Post.company_id == task.company_id, Post.status == "published",
                                                    Post.published_at >= since))).all()
        for p in posts:
            a = await analytics.post_analytics(s, p)
            if not a.get("metrics"):
                continue
            found += 1
            prev = (p.meta or {}).get("last_metrics")
            if prev != a["metrics"]:
                p.meta = {**(p.meta or {}), "last_metrics": a["metrics"], "metrics_at": a.get("collected_at")}
                views = a["metrics"].get("views")
                await mem.remember(s, task.company_id, f"Результат поста «{p.topic or p.id}» ({p.platform}): "
                                   + ", ".join(f"{k} {v}" for k, v in a["metrics"].items()
                                               if k in ("views", "likes", "reposts", "comments", "reactions_total")),
                                   kind="episodic", category="analysis",
                                   meta={"post_id": str(p.id), "views": views})
        await s.commit()
    return {"posts": len(posts), "with_metrics": found}, f"📈 Собраны показатели {found} из {len(posts)} публикаций"


async def _run_comments(sm, task: ScheduledTask, deps: TaskDeps) -> tuple[dict[str, Any], str]:
    from . import inbox

    res = await inbox.collect_vk_comments(sm, task.company_id)
    res["auto_replied"] = await inbox.auto_reply(sm, task.company_id, deps)
    return res, f"💬 Новых комментариев: {res.get('new', 0)}, автоответов: {res['auto_replied']}"


async def _run_agent(sm, task: ScheduledTask, deps: TaskDeps) -> tuple[dict[str, Any], str]:
    if deps.orchestrator is None:
        raise RuntimeError("Агент не настроен")
    async with sm() as s:
        r = await deps.orchestrator.chat(s, task.company_id, task.params["prompt"],
                                         meta={"scheduled_task_id": str(task.id), "source": "schedule"})
    prefix = "[ТЕСТОВАЯ МОДЕЛЬ] " if r.is_mock else ""
    return ({"message_id": r.message_id, "steps": [st["tool"] for st in r.steps], "is_mock": r.is_mock},
            f"🤖 {task.title}:\n{prefix}{r.answer}")


RUNNERS = {"report": _run_report, "sync_sources": _run_sync, "collect_results": _run_collect_results,
           "collect_comments": _run_comments, "agent": _run_agent}


async def _claim(sm: async_sessionmaker[AsyncSession], task_id: uuid.UUID, now: datetime, *, force: bool) -> bool:
    async with sm() as s, s.begin():
        cond = [ScheduledTask.id == task_id,
                or_(ScheduledTask.running_since.is_(None), ScheduledTask.running_since < now - STUCK_AFTER)]
        if not force:
            cond += [ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now]
        res = await s.execute(update(ScheduledTask).where(*cond).values(running_since=now))
        return res.rowcount == 1


async def run_task(sm: async_sessionmaker[AsyncSession], task_id: uuid.UUID, deps: TaskDeps, *,
                   force: bool = False, now: datetime | None = None) -> str:
    """Выполняет задачу (атомарный захват — без двойного запуска). Возвращает ok | error | skipped."""
    now = now or datetime.now(timezone.utc)
    if not await _claim(sm, task_id, now, force=force):
        return "skipped"
    async with sm() as s:
        task = await s.get(ScheduledTask, task_id)
        company = await s.get(Company, task.company_id)
    status, result, text = "ok", {}, ""
    try:
        result, text = await RUNNERS[task.kind](sm, task, deps)
    except AIError as e:
        status, result = "error", {"error": e.to_dict()}
        text = f"⚠️ Задача «{task.title}» не выполнена: {e.code} — {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("Задача %s упала", task_id)
        status, result = "error", {"error": f"{type(e).__name__}: {e}"}
        text = f"⚠️ Задача «{task.title}» не выполнена: {type(e).__name__}: {e}"
    async with sm() as s, s.begin():
        t = await s.get(ScheduledTask, task_id)
        t.running_since = None
        t.last_run_at = now
        t.last_status = status
        t.last_result = result
        t.run_count += 1
        t.next_run_at = next_run(t.schedule, max(now, datetime.now(timezone.utc)))
        s.add(AuditLog(company_id=t.company_id, action="task.run",
                       details={"task_id": str(t.id), "kind": t.kind, "status": status, "forced": force}))
    try:
        async with sm() as s:
            if status == "error" or task.notify:
                await deps.notifier.notify(s, task.company_id, text)
            if status == "error" and hasattr(deps.notifier, "notify_admins"):
                await deps.notifier.notify_admins(f"[{company.name}] {text}")
    except Exception:  # noqa: BLE001
        log.exception("Уведомление о задаче не отправлено")
    return status


async def run_due_tasks(sm: async_sessionmaker[AsyncSession], deps: TaskDeps, *, now: datetime | None = None,
                        limit: int = 20) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    async with sm() as s:
        ids = (await s.scalars(select(ScheduledTask.id).where(
            ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now)
            .order_by(ScheduledTask.next_run_at).limit(limit))).all()
    return {str(i): await run_task(sm, i, deps, now=now) for i in ids}


async def scheduler_loop_all(sm: async_sessionmaker[AsyncSession], publisher: Any, deps: TaskDeps, *,
                             interval: float = 30, stop=None) -> None:
    """Один цикл планировщика: публикации по расписанию + регулярные задачи компаний."""
    import asyncio

    from .publisher import recover_stuck, run_due

    stop = stop or asyncio.Event()
    log.info("Планировщик запущен, интервал %s с", interval)
    while not stop.is_set():
        try:
            await recover_stuck(sm)
            if publisher is not None:
                done = await run_due(sm, publisher, deps.notifier)
                if done:
                    log.info("Публикации: %s", done)
            done = await run_due_tasks(sm, deps)
            if done:
                log.info("Задачи: %s", done)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка цикла планировщика")
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            pass
