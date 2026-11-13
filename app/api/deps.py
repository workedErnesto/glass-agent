"""Зависимости FastAPI: сессия БД, текущий пользователь, проверка доступа к компании."""
from __future__ import annotations

import logging
import uuid
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Company, CompanyMember, User

from .auth import InitDataError, validate_init_data

log = logging.getLogger(__name__)
ROLE_RANK = {"viewer": 0, "editor": 1, "approver": 2, "owner": 3}


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    s = get_settings()
    auth = request.headers.get("authorization", "")
    tg_user: dict | None = None
    if auth.lower().startswith("tma "):
        token = request.app.state.bot_token
        if not token:
            raise HTTPException(500, "TG_BOT_TOKEN не задан — проверка initData невозможна")
        try:
            tg_user = validate_init_data(auth[4:].strip(), token, max_age_s=s.initdata_max_age_s)["user"]
        except InitDataError as e:
            raise HTTPException(401, f"Неверные initData: {e}") from e
    elif s.api_dev_user_id is not None and not auth:
        tg_user = {"id": s.api_dev_user_id, "first_name": "dev"}  # только локальная разработка
    else:
        raise HTTPException(401, "Нужна авторизация Telegram Mini App: Authorization: tma <initData>")
    user = await session.scalar(select(User).where(User.telegram_id == tg_user["id"]))
    name = " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x) or \
        tg_user.get("username") or ""
    is_admin = tg_user["id"] in s.admin_ids
    if user is None:
        user = User(telegram_id=tg_user["id"], name=name[:200], is_admin=is_admin)
        session.add(user)
        await session.commit()
    elif user.is_admin != is_admin or (name and user.name != name[:200]):
        user.is_admin, user.name = is_admin, name[:200] or user.name
        await session.commit()
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Только администратор платформы")
    return user


async def company_access(session: AsyncSession, user: User, company_id: uuid.UUID, min_role: str) -> Company:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Компания не найдена")
    if user.is_admin:
        return company
    role = await session.scalar(select(CompanyMember.role).where(CompanyMember.company_id == company_id,
                                                                  CompanyMember.user_id == user.id))
    if role is None:
        raise HTTPException(404, "Компания не найдена")  # не раскрываем существование чужих компаний
    if ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
        raise HTTPException(403, f"Недостаточно прав: нужна роль {min_role}, у вас {role}")
    return company


def access(min_role: str):
    async def dep(company_id: uuid.UUID, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)) -> Company:
        return await company_access(session, user, company_id, min_role)
    return dep
