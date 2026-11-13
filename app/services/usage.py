"""Учёт расхода вызовов модели и дневной лимит на компанию (контроль расходов, запрет несанкционированных трат).

Лимит: settings.ai_daily_call_limit компании, иначе AI_DAILY_CALL_LIMIT (0 = без лимита).
Подписка ChatGPT Plus оплачивается фиксированно, но её 5-часовое окно общее для всех
компаний администратора — лимит не даёт одной компании выбрать его целиком.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.connectors.base import AIError, AIResult
from app.core.config import get_settings
from app.db.models import AIUsage, Company


class CompanyQuotaExceeded(AIError):
    code = "AI_COMPANY_QUOTA_EXCEEDED"


def _today() -> date:
    return datetime.now(timezone.utc).date()


async def calls_today(session: AsyncSession, company_id: uuid.UUID) -> int:
    return int(await session.scalar(select(func.coalesce(func.sum(AIUsage.calls), 0)).where(
        AIUsage.company_id == company_id, AIUsage.day == _today())) or 0)


async def daily_limit(session: AsyncSession, company_id: uuid.UUID) -> int:
    company = await session.get(Company, company_id)
    value = (company.settings or {}).get("ai_daily_call_limit") if company else None
    return int(value) if value is not None else get_settings().ai_daily_call_limit


async def check_quota(session: AsyncSession, company_id: uuid.UUID) -> None:
    limit = await daily_limit(session, company_id)
    if limit and await calls_today(session, company_id) >= limit:
        raise CompanyQuotaExceeded(f"Дневной лимит вызовов AI для компании исчерпан ({limit}). "
                                   "Изменить: settings.ai_daily_call_limit", reason="company_daily_limit")


async def record(session: AsyncSession, company_id: uuid.UUID | None, res: AIResult) -> None:
    usage = res.usage or {}
    stmt = insert(AIUsage).values(company_id=company_id, day=_today(), provider=res.provider or "unknown", calls=1,
                                  input_tokens=int(usage.get("input_tokens") or 0),
                                  output_tokens=int(usage.get("output_tokens") or 0))
    stmt = stmt.on_conflict_do_update(constraint="ai_usage_company_id_day_provider_key", set_={
        "calls": AIUsage.calls + 1, "input_tokens": AIUsage.input_tokens + stmt.excluded.input_tokens,
        "output_tokens": AIUsage.output_tokens + stmt.excluded.output_tokens})
    await session.execute(stmt)


async def report(session: AsyncSession, company_id: uuid.UUID | None = None, days: int = 30) -> list[dict[str, Any]]:
    since = _today() - timedelta(days=days - 1)
    q = select(AIUsage.company_id, Company.name, AIUsage.provider, func.sum(AIUsage.calls),
               func.sum(AIUsage.input_tokens), func.sum(AIUsage.output_tokens)).join(
        Company, Company.id == AIUsage.company_id, isouter=True).where(AIUsage.day >= since)
    if company_id:
        q = q.where(AIUsage.company_id == company_id)
    rows = (await session.execute(q.group_by(AIUsage.company_id, Company.name, AIUsage.provider))).all()
    return [{"company_id": str(cid) if cid else None, "company": name, "provider": prov, "calls": int(c or 0),
             "input_tokens": int(i or 0), "output_tokens": int(o or 0), "days": days} for cid, name, prov, c, i, o in rows]
