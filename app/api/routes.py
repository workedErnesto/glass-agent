"""REST API для Telegram Mini App (Vue 3). Контракт и сценарии — docs/FRONTEND.md."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.connectors.base import AIError, AIUsageLimitExceeded
from app.ai.tools import Budget, ToolContext, ToolError, call_tool, create_post_draft
from app.core.config import get_settings
from app.db.models import AuditLog, Company, CompanyMember, Document, InboxItem, ScheduledTask, Source, User
from app.services import analytics, images, inbox as inbox_svc, memory as mem, posts as P, secrets as secrets_svc
from app.services import tasks as tasks_svc, usage as usage_svc
from app.services.sources import SOURCE_TYPES, add_source, create_company, slugify, sync_source

from . import schemas as S
from .deps import ROLE_RANK, access, current_user, get_session, require_admin

log = logging.getLogger(__name__)
ERR = {401: {"model": S.ErrorOut}, 403: {"model": S.ErrorOut}, 404: {"model": S.ErrorOut}}
AI_ERR = {**ERR, 429: {"model": S.ErrorOut}, 503: {"model": S.ErrorOut}}
router = APIRouter(prefix="/api", responses=ERR)


def ai_error_response(e: AIError) -> JSONResponse:
    if isinstance(e, (AIUsageLimitExceeded, usage_svc.CompanyQuotaExceeded)):
        status = 429
    elif e.code == "AI_REQUEST_ERROR":
        status = 502
    else:
        status = 503
    return JSONResponse(status_code=status, content={"error": e.to_dict()})


def _ctx(request: Request, session: AsyncSession, company: Company, user: User, calls: int = 2) -> ToolContext:
    st = request.app.state
    return ToolContext(session=session, company_id=company.id, connector=st.connector, searcher=st.searcher,
                       user_id=user.id, budget=Budget(max_llm_calls=calls), notifier=st.notifier)


async def _run_tool(request, session, company, user, name: str, args: dict[str, Any], calls: int = 2):
    ctx = _ctx(request, session, company, user, calls)
    try:
        result = await call_tool(ctx, name, args)
    except AIError as e:
        await session.rollback()
        return ai_error_response(e)
    except ToolError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    for fn in ctx.after_commit:
        await fn(session)
    return {"result": result, "ai": ctx.ai_meta()}


# --------------------------------------------------------------------------- справочники и пользователь

@router.get("/meta", tags=["system"])
async def meta():
    """Значения перечислений для фронтенда."""
    return {"post_statuses": P.STATUSES, "platforms": P.PLATFORMS, "roles": list(ROLE_RANK),
            "task_kinds": tasks_svc.KINDS, "source_types": sorted(SOURCE_TYPES - {"file"}),
            "secret_names": secrets_svc.ALLOWED, "image_templates": list(images.TEMPLATES),
            "inbox_statuses": ["new", "draft_ready", "replied", "ignored", "error"],
            "memory_categories": sorted(mem.LONG_TERM_CATEGORIES)}


@router.get("/me", response_model=S.MeOut, tags=["system"])
async def me(user: User = Depends(current_user)):
    return {"telegram_id": user.telegram_id, "name": user.name, "is_admin": user.is_admin}


# --------------------------------------------------------------------------- компании

class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,59}$")
    profile: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    owner_telegram_id: int | None = None  # представитель компании, получит роль owner


class CompanyPatch(BaseModel):
    name: str | None = None
    profile: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


def company_dict(c: Company, role: str | None = None) -> dict[str, Any]:
    return {"id": str(c.id), "slug": c.slug, "name": c.name, "profile": c.profile, "settings": c.settings,
            "role": role, "created_at": c.created_at.isoformat() if c.created_at else None}


async def _ensure_user(session: AsyncSession, telegram_id: int) -> User:
    u = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if u is None:
        u = User(telegram_id=telegram_id, prefs={})
        session.add(u)
        await session.flush()
    return u


@router.post("/companies", status_code=201, response_model=S.CompanyOut, tags=["companies"])
async def create_company_ep(body: CompanyIn, user: User = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    """Подключение новой компании — администратор платформы (п. 2 и 4 ТЗ). Создаются задачи по умолчанию."""
    slug = body.slug or slugify(body.name)
    if await session.scalar(select(Company.id).where(Company.slug == slug)):
        raise HTTPException(409, f"slug {slug!r} уже занят")
    c = await create_company(session, body.name, slug=slug, profile=body.profile, settings=body.settings,
                             user_id=user.id)
    session.add(CompanyMember(company_id=c.id, user_id=user.id, role="owner"))
    if body.owner_telegram_id and body.owner_telegram_id != user.telegram_id:
        owner = await _ensure_user(session, body.owner_telegram_id)
        session.add(CompanyMember(company_id=c.id, user_id=owner.id, role="owner"))
    await session.commit()
    return company_dict(c, "owner")


@router.get("/companies", response_model=list[S.CompanyOut], tags=["companies"])
async def list_companies(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    if user.is_admin:
        rows = [(c, "admin") for c in (await session.scalars(select(Company).order_by(Company.created_at))).all()]
    else:
        rows = (await session.execute(select(Company, CompanyMember.role).join(
            CompanyMember, CompanyMember.company_id == Company.id).where(CompanyMember.user_id == user.id)
            .order_by(Company.created_at))).all()
    return [company_dict(c, role) for c, role in rows]


@router.get("/companies/{company_id}", response_model=S.CompanyOut, tags=["companies"])
async def get_company(company: Company = Depends(access("viewer"))):
    return company_dict(company)


@router.patch("/companies/{company_id}", response_model=S.CompanyOut, tags=["companies"])
async def patch_company(body: CompanyPatch, company: Company = Depends(access("owner")),
                        user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Профиль и настройки AI-маркетолога: channels, brand, permissions, approval, ai_daily_call_limit — см. docs."""
    if body.name is not None:
        company.name = body.name
    if body.profile is not None:
        company.profile = body.profile
    if body.settings is not None:
        company.settings = body.settings
    session.add(AuditLog(company_id=company.id, user_id=user.id, action="company.update",
                         details={"fields": [k for k, v in body.model_dump().items() if v is not None]}))
    await session.commit()
    return company_dict(company)


class MemberIn(BaseModel):
    telegram_id: int
    role: str = Field(pattern="^(owner|approver|editor|viewer)$")


@router.get("/companies/{company_id}/members", response_model=list[S.MemberOut], tags=["companies"])
async def list_members(company: Company = Depends(access("viewer")), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(User, CompanyMember.role).join(
        CompanyMember, CompanyMember.user_id == User.id).where(CompanyMember.company_id == company.id))).all()
    return [{"telegram_id": u.telegram_id, "name": u.name, "role": r} for u, r in rows]


@router.post("/companies/{company_id}/members", response_model=S.MemberOut, tags=["companies"])
async def add_member(body: MemberIn, company: Company = Depends(access("owner")), user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    u = await _ensure_user(session, body.telegram_id)
    m = await session.get(CompanyMember, (company.id, u.id))
    if m is None:
        session.add(CompanyMember(company_id=company.id, user_id=u.id, role=body.role))
    else:
        m.role = body.role
    session.add(AuditLog(company_id=company.id, user_id=user.id, action="member.set",
                         details={"telegram_id": body.telegram_id, "role": body.role}))
    await session.commit()
    return {"telegram_id": body.telegram_id, "name": u.name, "role": body.role}


@router.delete("/companies/{company_id}/members/{telegram_id}", tags=["companies"])
async def remove_member(telegram_id: int, company: Company = Depends(access("owner")),
                        user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Отзыв доступа участника."""
    u = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    m = await session.get(CompanyMember, (company.id, u.id)) if u else None
    if m is None:
        raise HTTPException(404, "Участник не найден")
    await session.delete(m)
    session.add(AuditLog(company_id=company.id, user_id=user.id, action="member.remove",
                         details={"telegram_id": telegram_id}))
    await session.commit()
    return {"removed": True}


class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


@router.get("/companies/{company_id}/secrets", response_model=list[S.SecretOut], tags=["companies"])
async def list_secrets(company: Company = Depends(access("owner")), session: AsyncSession = Depends(get_session)):
    """Подключённые токены соцсетей (значения не возвращаются)."""
    return await secrets_svc.list_secrets(session, company.id)


@router.put("/companies/{company_id}/secrets/{name}", tags=["companies"])
async def put_secret(name: str, body: SecretIn, company: Company = Depends(access("owner")),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    try:
        await secrets_svc.set_secret(session, company.id, name, body.value, user_id=user.id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return {"name": name, "saved": True}


@router.delete("/companies/{company_id}/secrets/{name}", tags=["companies"])
async def delete_secret(name: str, company: Company = Depends(access("owner")), user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    """Отзыв токена соцсети."""
    if not await secrets_svc.delete_secret(session, company.id, name, user_id=user.id):
        raise HTTPException(404, "Секрет не найден")
    await session.commit()
    return {"deleted": True}


@router.get("/companies/{company_id}/audit", response_model=list[S.AuditOut], tags=["companies"])
async def audit(limit: int = 100, company: Company = Depends(access("owner")),
                session: AsyncSession = Depends(get_session)):
    """Журнал действий компании."""
    rows = (await session.scalars(select(AuditLog).where(AuditLog.company_id == company.id)
                                  .order_by(AuditLog.id.desc()).limit(max(1, min(500, limit))))).all()
    return [{"id": r.id, "action": r.action, "user_id": r.user_id, "details": r.details,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


@router.get("/companies/{company_id}/usage", response_model=list[S.UsageOut], tags=["companies"])
async def company_usage(days: int = 30, company: Company = Depends(access("owner")),
                        session: AsyncSession = Depends(get_session)):
    return await usage_svc.report(session, company.id, days=max(1, min(365, days)))


@router.get("/admin/usage", response_model=list[S.UsageOut], tags=["admin"])
async def admin_usage(days: int = 30, user: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Расход вызовов AI по всем компаниям (контроль расходов администратором)."""
    return await usage_svc.report(session, None, days=max(1, min(365, days)))


# --------------------------------------------------------------------------- подключение AI

@router.get("/ai/connection", tags=["ai"])
async def ai_connection(request: Request, user: User = Depends(current_user)):
    """Статус подключения ChatGPT Plus (п. 12 критериев ТЗ). Без подключения: error=AI_CONNECTION_UNAVAILABLE."""
    st = await request.app.state.connector.status()
    if not user.is_admin:  # email и client_id видит только администратор
        st = {k: v for k, v in st.items() if k in ("provider", "connected", "plan_usage_permission", "error",
                                                   "reason", "paused_until", "uses_paid_api_key", "is_mock",
                                                   "warning")}
    login = request.app.state.login_session
    if login is not None and user.is_admin:
        st["login"] = login.public()
    return st


@router.get("/ai/models", tags=["ai"])
async def ai_models(request: Request, user: User = Depends(require_admin)):
    list_models = getattr(request.app.state.connector, "list_models", None)
    if list_models is None:
        return {"models": [], "note": "Коннектор не поддерживает список моделей"}
    try:
        return {"models": await list_models()}
    except AIError as e:
        return ai_error_response(e)


@router.post("/ai/connect", tags=["ai"])
async def ai_connect(request: Request, user: User = Depends(require_admin)):
    from app.ai.auth.flow import start_login

    s = get_settings()
    cur = request.app.state.login_session
    if cur is not None and cur.status == "waiting":
        return cur.public()
    tm = getattr(request.app.state.connector, "tokens", None)
    if tm is None:
        raise HTTPException(400, "Текущий коннектор не поддерживает вход ChatGPT")
    sess = await start_login(tm, host_id_path=s.chatgpt_host_id_path, agent_name=s.chatgpt_agent_name,
                             port=s.chatgpt_callback_port)
    request.app.state.login_session = sess
    return {**sess.public(), "note": "Откройте ссылку в браузере на той же машине, где запущен backend "
                                    "(redirect на 127.0.0.1). Затем проверьте GET /api/ai/connection."}


@router.post("/ai/disconnect", tags=["ai"])
async def ai_disconnect(request: Request, user: User = Depends(require_admin)):
    disconnect = getattr(request.app.state.connector, "disconnect", None)
    if disconnect is None:
        raise HTTPException(400, "Коннектор не поддерживает отключение")
    await disconnect()
    return {"connected": False}


# --------------------------------------------------------------------------- чат и память

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


def message_dict(m) -> dict[str, Any]:
    return {"id": m.id, "role": m.role, "content": m.content, "provider": m.provider, "model": m.model,
            "is_mock": m.is_mock, "meta": m.meta, "created_at": m.created_at.isoformat() if m.created_at else None}


@router.post("/companies/{company_id}/chat", response_model=S.ChatReplyOut, responses=AI_ERR, tags=["chat"])
async def chat(body: ChatIn, request: Request, company: Company = Depends(access("editor")),
               user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Сообщение в постоянный AI-чат компании. Агент сам вызывает инструменты (steps)."""
    try:
        r = await request.app.state.orchestrator.chat(session, company.id, body.message, user_id=user.id)
    except AIError as e:
        return ai_error_response(e)
    return r.__dict__


@router.get("/companies/{company_id}/chat/history", response_model=list[S.MessageOut], tags=["chat"])
async def chat_history(limit: int = 50, before_id: int | None = None, company: Company = Depends(access("viewer")),
                       session: AsyncSession = Depends(get_session)):
    msgs = await mem.history(session, company.id, limit=max(1, min(200, limit)), before_id=before_id)
    return [message_dict(m) for m in msgs]


@router.get("/companies/{company_id}/chat/memory", response_model=S.MemoryOut, tags=["chat"])
async def chat_memory(company: Company = Depends(access("viewer")), session: AsyncSession = Depends(get_session)):
    return await mem.load_memory(session, company.id)


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: str = "fact"


@router.post("/companies/{company_id}/chat/memory", status_code=201, tags=["chat"])
async def add_memory(body: MemoryIn, company: Company = Depends(access("editor")),
                     session: AsyncSession = Depends(get_session)):
    """Добавить факт в долговременную память (стиль бренда, цель, ограничение…)."""
    m = await mem.remember(session, company.id, body.content, category=body.category)
    await session.commit()
    return {"id": m.id if m else None, "created": m is not None}


@router.delete("/companies/{company_id}/chat/memory/{memory_id}", tags=["chat"])
async def delete_memory(memory_id: int, company: Company = Depends(access("editor")),
                        session: AsyncSession = Depends(get_session)):
    if not await mem.forget(session, company.id, memory_id):
        raise HTTPException(404, "Запись памяти не найдена")
    await session.commit()
    return {"deleted": True}


# --------------------------------------------------------------------------- источники и знания

API_SOURCE_TYPES = SOURCE_TYPES - {"file"}  # файлы — через загрузку


class SourceIn(BaseModel):
    type: str
    ref: str = Field(min_length=1, max_length=1000)
    name: str = ""
    is_competitor: bool = False
    options: dict[str, Any] = Field(default_factory=dict)
    sync_now: bool = False


def source_dict(s: Source) -> dict[str, Any]:
    return {"id": str(s.id), "type": s.type, "ref": s.ref if s.type != "file" else Path(s.ref).name, "name": s.name,
            "is_competitor": s.is_competitor, "status": s.status, "options": s.options, "last_error": s.last_error,
            "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None, "last_result": s.last_result}


async def _sync_bg(app, source_id: uuid.UUID, company_id: uuid.UUID) -> None:
    try:
        await sync_source(app.state.sessionmaker, source_id, app.state.embedder, company_id=company_id)
    except Exception:  # noqa: BLE001 — ошибка уже записана в source.last_error
        log.exception("Синхронизация %s не удалась", source_id)


@router.post("/companies/{company_id}/sources", status_code=201, response_model=S.SourceOut, tags=["knowledge"])
async def create_source(body: SourceIn, request: Request, background: BackgroundTasks,
                        company: Company = Depends(access("editor")), user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    """Источник знаний: website, telegram_web (открытый канал), telegram (Telethon), telegram_bot, vk.
    is_competitor=true — данные конкурента (ищутся отдельно)."""
    if body.type not in API_SOURCE_TYPES:
        raise HTTPException(422, f"type должен быть одним из {sorted(API_SOURCE_TYPES)}; файлы — POST .../files")
    src = await add_source(session, company.id, body.type, body.ref, name=body.name,
                           is_competitor=body.is_competitor, options=body.options, user_id=user.id)
    await session.commit()
    if body.sync_now:
        background.add_task(_sync_bg, request.app, src.id, company.id)
    return source_dict(src)


@router.get("/companies/{company_id}/sources", response_model=list[S.SourceOut], tags=["knowledge"])
async def list_sources(company: Company = Depends(access("viewer")), session: AsyncSession = Depends(get_session)):
    return [source_dict(s) for s in (await session.scalars(select(Source).where(Source.company_id == company.id)
                                                            .order_by(Source.created_at))).all()]


@router.delete("/companies/{company_id}/sources/{source_id}", tags=["knowledge"])
async def delete_source(source_id: uuid.UUID, company: Company = Depends(access("editor")),
                        user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Удаляет источник вместе с его документами и фрагментами RAG."""
    src = await session.scalar(select(Source).where(Source.id == source_id, Source.company_id == company.id))
    if src is None:
        raise HTTPException(404, "Источник не найден")
    await session.execute(delete(Document).where(Document.source_id == src.id))
    await session.delete(src)
    session.add(AuditLog(company_id=company.id, user_id=user.id, action="source.delete",
                         details={"type": src.type, "ref": src.ref}))
    await session.commit()
    return {"deleted": True}


@router.post("/companies/{company_id}/sources/{source_id}/sync", status_code=202, tags=["knowledge"])
async def sync_source_ep(source_id: uuid.UUID, request: Request, background: BackgroundTasks,
                         company: Company = Depends(access("editor")), session: AsyncSession = Depends(get_session)):
    """Запуск синхронизации в фоне; статус — GET .../sources (status: syncing → ok | error)."""
    src = await session.scalar(select(Source).where(Source.id == source_id, Source.company_id == company.id))
    if src is None:
        raise HTTPException(404, "Источник не найден")
    if src.status == "syncing":
        raise HTTPException(409, "Синхронизация уже выполняется")
    background.add_task(_sync_bg, request.app, src.id, company.id)
    return {"source_id": str(src.id), "status": "started"}


@router.post("/companies/{company_id}/files", status_code=202, tags=["knowledge"])
async def upload_file(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                      company: Company = Depends(access("editor")), user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """Загрузка документа (PDF/DOCX/TXT/MD) в базу знаний; индексация в фоне."""
    from app.parsers.file_parser import SUPPORTED

    s = get_settings()
    name = re.sub(r"[^\w.\- ]+", "_", Path(file.filename or "file").name).strip(" .") or "file"
    if Path(name).suffix.lower() not in SUPPORTED:
        raise HTTPException(422, f"Поддерживаются: {', '.join(sorted(SUPPORTED))}")
    data = await file.read(s.upload_max_mb * 1024 * 1024 + 1)
    if len(data) > s.upload_max_mb * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {s.upload_max_mb} МБ")
    root = (Path(s.data_dir) / company.slug / "files").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    src = await add_source(session, company.id, "file", str(root), name="Документы компании", user_id=user.id)
    await session.commit()
    background.add_task(_sync_bg, request.app, src.id, company.id)
    return {"file": name, "source_id": str(src.id), "status": "indexing"}


@router.get("/companies/{company_id}/files", tags=["knowledge"])
async def list_files(company: Company = Depends(access("viewer"))):
    root = Path(get_settings().data_dir) / company.slug / "files"
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(root.glob("*")) if p.is_file()] \
        if root.is_dir() else []


@router.delete("/companies/{company_id}/files/{name}", tags=["knowledge"])
async def delete_file(name: str, request: Request, background: BackgroundTasks,
                      company: Company = Depends(access("editor")), session: AsyncSession = Depends(get_session)):
    root = (Path(get_settings().data_dir) / company.slug / "files").resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    path.unlink()
    src = await session.scalar(select(Source).where(Source.company_id == company.id, Source.type == "file"))
    if src:
        background.add_task(_sync_bg, request.app, src.id, company.id)  # документ уйдёт из базы знаний
    return {"deleted": True}


@router.post("/companies/{company_id}/media", status_code=201, tags=["content"])
async def upload_media(file: UploadFile = File(...), company: Company = Depends(access("editor"))):
    """Медиатека компании: фоны и логотип для шаблонов изображений (PNG/JPG/WEBP)."""
    s = get_settings()
    name = re.sub(r"[^\w.\-]+", "_", Path(file.filename or "image").name).strip(".") or "image"
    if Path(name).suffix.lower() not in images.IMAGE_EXT:
        raise HTTPException(422, "Поддерживаются PNG, JPG, WEBP")
    data = await file.read(s.upload_max_mb * 1024 * 1024 + 1)
    if len(data) > s.upload_max_mb * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {s.upload_max_mb} МБ")
    root = images.media_dir(s.data_dir, company.slug)
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    return {"file": name}


@router.get("/companies/{company_id}/media", tags=["content"])
async def media(company: Company = Depends(access("viewer"))):
    s = get_settings()
    return {"files": images.list_media(s.data_dir, company.slug), "templates": list(images.TEMPLATES),
            "local_generation": images.local_generation_status(s.local_image_model)}


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=30)
    scope: str = Field(default="own", pattern="^(own|competitors|all)$")


@router.post("/companies/{company_id}/knowledge/search", response_model=list[S.SearchHitOut], tags=["knowledge"])
async def knowledge_search(body: SearchIn, request: Request, company: Company = Depends(access("viewer")),
                           session: AsyncSession = Depends(get_session)):
    competitor = {"own": False, "competitors": True, "all": None}[body.scope]
    hits = await request.app.state.searcher.search(session, company.id, body.query, limit=body.limit,
                                                    competitor=competitor)
    return [{**h.to_dict(), "citation": h.citation()} for h in hits]


# --------------------------------------------------------------------------- контент (AI)

class GenerateIn(BaseModel):
    topic: str = Field(min_length=1, max_length=1000)
    platform: str = Field(default="telegram", pattern="^(telegram|vk|max)$")
    image: bool = False
    proposed_at: datetime | None = None
    submit: bool = False  # сразу проверить и отправить на согласование


@router.post("/companies/{company_id}/posts/generate", status_code=201, response_model=S.PostOut, responses=AI_ERR,
             tags=["posts"])
async def generate_post(body: GenerateIn, request: Request, company: Company = Depends(access("editor")),
                        user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    ctx = _ctx(request, session, company, user)
    try:
        res = await create_post_draft(ctx, body.topic, body.platform)
    except AIError as e:
        await session.rollback()
        return ai_error_response(e)
    except ToolError as e:
        raise HTTPException(422, str(e)) from e
    post = await P.get_post(session, company.id, res["post_id"])
    post.proposed_at = body.proposed_at
    if body.image:
        r = images.render_for_company(get_settings().data_dir, company.slug, company.settings or {},
                                      f"{post.id}-r1", title=body.topic[:80])
        post.image_path, post.image_meta = str(r.path), r.meta
    if body.submit:
        try:
            await P.submit(session, post, user_id=user.id)
        except P.PostStateError as e:
            post.last_error = str(e)  # черновик сохраняется, причина видна во фронтенде
    await session.commit()
    if post.status == P.PENDING:
        await _notify_approval(request, session, post)
    return P.post_to_dict(post)


class ContentIn(BaseModel):
    topic: str = ""
    period: str = "неделя"
    duration: int = Field(default=30, ge=10, le=180)
    focus: str = ""
    count: int = Field(default=5, ge=1, le=15)


@router.post("/companies/{company_id}/content/{kind}", responses=AI_ERR, tags=["content"])
async def content(kind: str, body: ContentIn, request: Request, company: Company = Depends(access("editor")),
                  user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """AI-материалы: strategy | content-plan | video-script | topics."""
    tools = {"strategy": ("generate_marketing_strategy", {"focus": body.focus}),
             "content-plan": ("generate_content_plan", {"period": body.period}),
             "video-script": ("create_video_script", {"topic": body.topic, "duration": body.duration}),
             "topics": ("suggest_topics", {"count": body.count})}
    if kind not in tools:
        raise HTTPException(404, f"Неизвестный вид: {', '.join(tools)}")
    name, args = tools[kind]
    return await _run_tool(request, session, company, user, name, args)


# --------------------------------------------------------------------------- посты и согласование

class PostIn(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    topic: str = ""
    platform: str = Field(default="telegram", pattern="^(telegram|vk|max)$")
    proposed_at: datetime | None = None


class PostPatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=16000)
    proposed_at: datetime | None = None
    platform: str | None = Field(default=None, pattern="^(telegram|vk|max)$")
    target_chat_id: str | None = None


class SubmitIn(BaseModel):
    proposed_at: datetime | None = None


class ApproveIn(BaseModel):
    scheduled_at: datetime | None = None  # пусто: proposed_at поста, если есть; иначе только согласовать
    publish_now: bool = False
    target_chat_id: str | None = None


class CommentIn(BaseModel):
    comment: str = ""


class ChangesIn(BaseModel):
    comment: str = Field(min_length=1, max_length=4000)
    auto_revise: bool = False  # AI сразу перепишет по комментарию и отправит на повторное согласование


class ScheduleIn(BaseModel):
    at: datetime | None = None  # пусто = сейчас


class ImageIn(BaseModel):
    title: str | None = None
    subtitle: str = ""
    template: str = "square"
    background: str | None = None


async def _post(session, company, post_id, for_update=False):
    try:
        return await P.get_post(session, company.id, post_id, for_update=for_update)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


async def _notify_approval(request: Request, session: AsyncSession, post) -> None:
    notifier = request.app.state.notifier
    try:
        if hasattr(notifier, "notify_approval"):
            await notifier.notify_approval(session, post)
        else:
            await notifier.notify(session, post.company_id, f"📝 На согласовании пост «{post.topic or post.text[:50]}»")
    except Exception:  # noqa: BLE001
        log.exception("Уведомление о согласовании не отправлено")


def _state_error(e: P.PostStateError) -> HTTPException:
    return HTTPException(409, str(e))


@router.post("/companies/{company_id}/posts", status_code=201, response_model=S.PostOut, tags=["posts"])
async def create_post(body: PostIn, company: Company = Depends(access("editor")), user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """Пост, написанный вручную (без AI)."""
    post = await P.create_draft(session, company.id, text=body.text, topic=body.topic, platform=body.platform,
                                user_id=user.id, generated_by={"provider": "human"}, proposed_at=body.proposed_at)
    await session.commit()
    return P.post_to_dict(post)


@router.get("/companies/{company_id}/posts", response_model=list[S.PostOut], tags=["posts"])
async def list_posts(status: str | None = None, company: Company = Depends(access("viewer")),
                     session: AsyncSession = Depends(get_session)):
    return [P.post_to_dict(p) for p in await P.list_posts(session, company.id, status=status, limit=100)]


@router.get("/companies/{company_id}/posts/{post_id}", response_model=S.PostOut, tags=["posts"])
async def get_post(post_id: uuid.UUID, company: Company = Depends(access("viewer")),
                   session: AsyncSession = Depends(get_session)):
    return P.post_to_dict(await _post(session, company, post_id))


@router.get("/companies/{company_id}/posts/{post_id}/image", tags=["posts"])
async def get_post_image(post_id: uuid.UUID, company: Company = Depends(access("viewer")),
                         session: AsyncSession = Depends(get_session)):
    post = await _post(session, company, post_id)
    if not post.image_path or not Path(post.image_path).is_file():
        raise HTTPException(404, "Изображения нет")
    return FileResponse(post.image_path, media_type="image/png")


@router.patch("/companies/{company_id}/posts/{post_id}", response_model=S.PostOut, tags=["posts"])
async def patch_post(post_id: uuid.UUID, body: PostPatch, company: Company = Depends(access("editor")),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Правка текста/площадки/канала. Изменение согласованного поста возвращает его в черновик."""
    post = await _post(session, company, post_id, for_update=True)
    try:
        if body.text is not None or body.platform is not None:
            await P.edit_content(session, post, text=body.text, platform=body.platform, user_id=user.id)
        if body.target_chat_id is not None:
            if post.status in (P.APPROVED, P.SCHEDULED):
                raise P.PostStateError("Канал согласованного поста менять нельзя — верните в черновик")
            post.target_chat_id = body.target_chat_id or None
    except P.PostStateError as e:
        raise _state_error(e) from e
    if body.proposed_at is not None:
        post.proposed_at = body.proposed_at
    await session.commit()
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/image", response_model=S.PostOut, tags=["posts"])
async def post_image(post_id: uuid.UUID, body: ImageIn, company: Company = Depends(access("editor")),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Картинка из фирменного шаблона (Pillow, image_meta.ai_generated=false)."""
    post = await _post(session, company, post_id, for_update=True)
    try:
        r = images.render_for_company(get_settings().data_dir, company.slug, company.settings or {},
                                      f"{post.id}-r{post.revision + 1}", title=body.title or post.topic or post.text[:60],
                                      subtitle=body.subtitle, template=body.template, background=body.background)
        await P.edit_content(session, post, image_path=str(r.path), image_meta=r.meta, user_id=user.id)
    except images.ImageError as e:
        raise HTTPException(422, str(e)) from e
    except P.PostStateError as e:
        raise _state_error(e) from e
    await session.commit()
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/check", tags=["posts"])
async def check_post(post_id: uuid.UUID, company: Company = Depends(access("viewer")),
                     session: AsyncSession = Depends(get_session)):
    """Проверка перед отправкой: errors блокируют согласование, warnings видит согласующий."""
    post = await _post(session, company, post_id)
    res = await P.run_checks(session, post)
    await session.commit()
    return res


@router.post("/companies/{company_id}/posts/{post_id}/submit", response_model=S.PostOut, tags=["posts"])
async def submit_post(post_id: uuid.UUID, request: Request, body: SubmitIn | None = None,
                      company: Company = Depends(access("editor")), user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """На согласование: согласующим (owner/approver) уходит сообщение в Telegram с материалом, площадкой и временем."""
    post = await _post(session, company, post_id, for_update=True)
    try:
        await P.submit(session, post, user_id=user.id, proposed_at=body.proposed_at if body else None)
    except P.PostStateError as e:
        await session.commit()  # результаты проверки сохраняем
        raise _state_error(e) from e
    await session.commit()
    await _notify_approval(request, session, post)
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/approve", response_model=S.PostOut, tags=["posts"])
async def approve_post(post_id: uuid.UUID, body: ApproveIn | None = None, company: Company = Depends(access("approver")),
                       user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    body = body or ApproveIn()
    post = await _post(session, company, post_id, for_update=True)
    at = datetime.now(timezone.utc) if body.publish_now else (body.scheduled_at or post.proposed_at)
    try:
        await P.approve(session, post, user_id=user.id, scheduled_at=at, target_chat_id=body.target_chat_id)
    except P.PostStateError as e:
        raise _state_error(e) from e
    await session.commit()
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/request-changes", response_model=S.PostOut, responses=AI_ERR,
             tags=["posts"])
async def request_changes(post_id: uuid.UUID, body: ChangesIn, request: Request,
                          company: Company = Depends(access("approver")), user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    """Возврат на доработку с комментарием; auto_revise — AI переписывает и отправляет повторно."""
    post = await _post(session, company, post_id, for_update=True)
    try:
        await P.request_changes(session, post, user_id=user.id, comment=body.comment)
    except P.PostStateError as e:
        raise _state_error(e) from e
    await session.commit()
    if body.auto_revise:
        ctx = _ctx(request, session, company, user)
        try:
            await call_tool(ctx, "update_post_draft", {"post_id": str(post.id), "instructions": body.comment})
            await P.submit(session, post, user_id=user.id)
            await session.commit()
            await _notify_approval(request, session, post)
        except AIError as e:
            await session.rollback()
            return ai_error_response(e)
        except (ToolError, P.PostStateError) as e:
            await session.rollback()
            raise HTTPException(409, str(e)) from e
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/reject", response_model=S.PostOut, tags=["posts"])
async def reject_post(post_id: uuid.UUID, body: CommentIn, company: Company = Depends(access("approver")),
                      user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    post = await _post(session, company, post_id, for_update=True)
    try:
        await P.reject(session, post, user_id=user.id, comment=body.comment)
    except P.PostStateError as e:
        raise _state_error(e) from e
    await session.commit()
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/schedule", response_model=S.PostOut, tags=["posts"])
async def schedule_post(post_id: uuid.UUID, body: ScheduleIn, company: Company = Depends(access("approver")),
                        user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    post = await _post(session, company, post_id, for_update=True)
    try:
        await P.schedule(session, post, body.at, user_id=user.id)
    except P.PostStateError as e:
        raise _state_error(e) from e
    await session.commit()
    return P.post_to_dict(post)


@router.post("/companies/{company_id}/posts/{post_id}/adapt", responses=AI_ERR, tags=["posts"])
async def adapt_post(post_id: uuid.UUID, platform: str, request: Request, company: Company = Depends(access("editor")),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Новый черновик этого поста для другой площадки (AI)."""
    return await _run_tool(request, session, company, user, "adapt_post", {"post_id": str(post_id),
                                                                          "platform": platform})


@router.get("/companies/{company_id}/posts/{post_id}/analytics", tags=["posts"])
async def post_analytics(post_id: uuid.UUID, company: Company = Depends(access("viewer")),
                         session: AsyncSession = Depends(get_session)):
    return await analytics.post_analytics(session, await _post(session, company, post_id))


# --------------------------------------------------------------------------- аналитика и отчёты

@router.get("/companies/{company_id}/analytics", tags=["analytics"])
async def company_analytics(company: Company = Depends(access("viewer")), session: AsyncSession = Depends(get_session)):
    return await analytics.company_analytics(session, company.id)


@router.get("/companies/{company_id}/analytics/competitors", tags=["analytics"])
async def competitors(company: Company = Depends(access("viewer")), session: AsyncSession = Depends(get_session)):
    return await analytics.competitors_comparison(session, company.id)


@router.get("/companies/{company_id}/reports", tags=["analytics"])
async def report(days: int = 7, company: Company = Depends(access("viewer")),
                 session: AsyncSession = Depends(get_session)):
    """Отчёт за период по собранным данным (без AI). Выводы AI — задача report или POST .../content/…"""
    rep = await analytics.period_report(session, company.id, days=max(1, min(365, days)))
    return {"report": rep, "text": analytics.report_text(company.name, rep)}


# --------------------------------------------------------------------------- регулярные задачи

class TaskIn(BaseModel):
    kind: str
    schedule: dict[str, Any]
    title: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    notify: bool = True


class TaskPatch(BaseModel):
    title: str | None = None
    schedule: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    notify: bool | None = None


async def _task(session, company, task_id) -> ScheduledTask:
    t = await session.scalar(select(ScheduledTask).where(ScheduledTask.id == task_id,
                                                         ScheduledTask.company_id == company.id))
    if t is None:
        raise HTTPException(404, "Задача не найдена")
    return t


@router.get("/companies/{company_id}/tasks", response_model=list[S.TaskOut], tags=["tasks"])
async def list_tasks(company: Company = Depends(access("viewer")), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(ScheduledTask).where(ScheduledTask.company_id == company.id)
                                  .order_by(ScheduledTask.created_at))).all()
    return [tasks_svc.task_dict(t) for t in rows]


@router.post("/companies/{company_id}/tasks", status_code=201, response_model=S.TaskOut, tags=["tasks"])
async def create_task(body: TaskIn, company: Company = Depends(access("owner")), user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    try:
        t = await tasks_svc.create_task(session, company.id, kind=body.kind, schedule=body.schedule, title=body.title,
                                        params=body.params, enabled=body.enabled, notify=body.notify, user_id=user.id)
    except tasks_svc.TaskConfigError as e:
        raise HTTPException(422, str(e)) from e
    await session.commit()
    return tasks_svc.task_dict(t)


@router.patch("/companies/{company_id}/tasks/{task_id}", response_model=S.TaskOut, tags=["tasks"])
async def patch_task(task_id: uuid.UUID, body: TaskPatch, company: Company = Depends(access("owner")),
                     session: AsyncSession = Depends(get_session)):
    t = await _task(session, company, task_id)
    try:
        if body.schedule is not None:
            t.schedule = tasks_svc.validate_schedule(body.schedule)
            t.next_run_at = tasks_svc.next_run(t.schedule, datetime.now(timezone.utc))
        if body.params is not None:
            if t.kind == "agent" and not body.params.get("prompt"):
                raise tasks_svc.TaskConfigError("Для agent нужен params.prompt")
            t.params = body.params
    except tasks_svc.TaskConfigError as e:
        raise HTTPException(422, str(e)) from e
    for f in ("title", "enabled", "notify"):
        if getattr(body, f) is not None:
            setattr(t, f, getattr(body, f))
    if body.enabled and t.next_run_at is None:
        t.next_run_at = tasks_svc.next_run(t.schedule, datetime.now(timezone.utc))
    await session.commit()
    return tasks_svc.task_dict(t)


@router.delete("/companies/{company_id}/tasks/{task_id}", tags=["tasks"])
async def delete_task(task_id: uuid.UUID, company: Company = Depends(access("owner")),
                      session: AsyncSession = Depends(get_session)):
    await session.delete(await _task(session, company, task_id))
    await session.commit()
    return {"deleted": True}


@router.post("/companies/{company_id}/tasks/{task_id}/run", response_model=S.TaskOut, tags=["tasks"])
async def run_task_now(task_id: uuid.UUID, request: Request, company: Company = Depends(access("editor")),
                       session: AsyncSession = Depends(get_session)):
    """Запустить задачу сейчас (синхронно; для долгих синхронизаций — до нескольких минут)."""
    await _task(session, company, task_id)
    status = await tasks_svc.run_task(request.app.state.sessionmaker, task_id, request.app.state.task_deps, force=True)
    if status == "skipped":
        raise HTTPException(409, "Задача уже выполняется")
    await session.close()
    async with request.app.state.sessionmaker() as s:
        return tasks_svc.task_dict(await s.get(ScheduledTask, task_id))


# --------------------------------------------------------------------------- комментарии и обращения

class ReplyIn(BaseModel):
    reply_text: str = Field(min_length=1, max_length=4000)


async def _item(session, company, item_id) -> InboxItem:
    i = await session.scalar(select(InboxItem).where(InboxItem.id == item_id, InboxItem.company_id == company.id))
    if i is None:
        raise HTTPException(404, "Сообщение не найдено")
    return i


@router.get("/companies/{company_id}/inbox", response_model=list[S.InboxItemOut], tags=["inbox"])
async def list_inbox(status: str | None = None, company: Company = Depends(access("viewer")),
                     session: AsyncSession = Depends(get_session)):
    q = select(InboxItem).where(InboxItem.company_id == company.id)
    if status:
        q = q.where(InboxItem.status == status)
    rows = (await session.scalars(q.order_by(InboxItem.received_at.desc().nulls_last()).limit(200))).all()
    return [inbox_svc.item_dict(i) for i in rows]


@router.post("/companies/{company_id}/inbox/{item_id}/draft", response_model=S.InboxItemOut, responses=AI_ERR,
             tags=["inbox"])
async def draft_reply(item_id: uuid.UUID, request: Request, company: Company = Depends(access("editor")),
                      session: AsyncSession = Depends(get_session)):
    """AI готовит черновик ответа (не отправляет)."""
    item = await _item(session, company, item_id)
    try:
        await inbox_svc.draft_reply(session, item, request.app.state.connector, request.app.state.searcher)
    except AIError as e:
        await session.rollback()
        return ai_error_response(e)
    await session.commit()
    return inbox_svc.item_dict(item)


@router.patch("/companies/{company_id}/inbox/{item_id}", response_model=S.InboxItemOut, tags=["inbox"])
async def edit_reply(item_id: uuid.UUID, body: ReplyIn, company: Company = Depends(access("editor")),
                     session: AsyncSession = Depends(get_session)):
    item = await _item(session, company, item_id)
    item.reply_text, item.status = body.reply_text, inbox_svc.DRAFT_READY
    item.reply_meta = {**(item.reply_meta or {}), "edited_by_human": True}
    await session.commit()
    return inbox_svc.item_dict(item)


@router.post("/companies/{company_id}/inbox/{item_id}/send", response_model=S.InboxItemOut, tags=["inbox"])
async def send_reply(item_id: uuid.UUID, company: Company = Depends(access("approver")),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Отправка ответа в соцсеть — подтверждение человеком с ролью approver."""
    from app.services.publisher import PublishError

    item = await _item(session, company, item_id)
    try:
        await inbox_svc.send_reply(session, item, user_id=user.id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except PublishError as e:
        await session.commit()
        raise HTTPException(502, f"Не отправлено: {e}") from e
    await session.commit()
    return inbox_svc.item_dict(item)


@router.post("/companies/{company_id}/inbox/{item_id}/ignore", response_model=S.InboxItemOut, tags=["inbox"])
async def ignore_item(item_id: uuid.UUID, company: Company = Depends(access("editor")),
                      session: AsyncSession = Depends(get_session)):
    item = await _item(session, company, item_id)
    item.status = inbox_svc.IGNORED
    await session.commit()
    return inbox_svc.item_dict(item)
