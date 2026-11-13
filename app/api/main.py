"""FastAPI-приложение Glass Agent.

    uvicorn app.api.main:app --host 127.0.0.1 --port 8000      # документация: /docs, схема: /openapi.json
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings

from .routes import router

log = logging.getLogger(__name__)


def create_app(*, sessionmaker=None, connector=None, embedder=None, searcher=None, notifier=None,
               publisher=None, bot_token: str | None = None, init_database: bool = True,
               cors_origins: list[str] | None = None) -> FastAPI:
    s = get_settings()

    def _wire(st: Any) -> None:
        """Достраивает зависимости, которые не переданы явно (orchestrator, notifier, publisher, task_deps)."""
        from app.services.publisher import LogNotifier, TelegramBotAPI, TelegramNotifier
        from app.services.tasks import TaskDeps

        if st.notifier is None:
            st.notifier = TelegramNotifier(TelegramBotAPI(st.bot_token)) if st.bot_token else LogNotifier()
        if st.publisher is None:
            from app.services.platforms import PublisherHub
            st.publisher = PublisherHub()
        if st.orchestrator is None and st.connector is not None and st.searcher is not None:
            from app.ai.orchestrator import Orchestrator
            st.orchestrator = Orchestrator(st.connector, st.searcher, max_steps=s.agent_max_steps,
                                           max_llm_calls=s.agent_max_llm_calls, max_seconds=s.agent_max_seconds,
                                           notifier=st.notifier)
        st.task_deps = TaskDeps(notifier=st.notifier, embedder=st.embedder, orchestrator=st.orchestrator,
                                publisher=st.publisher)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = app.state
        if st.sessionmaker is None:
            from app.db.session import get_sessionmaker
            st.sessionmaker = get_sessionmaker()
        if init_database:
            from app.db.session import get_engine, init_db
            await init_db(st.sessionmaker.kw.get("bind") or get_engine())
        if st.connector is None:
            from app.ai.factory import get_ai_connector
            st.connector = get_ai_connector()  # ChatGPT Plus; без подключения → AI_CONNECTION_UNAVAILABLE
        if st.embedder is None:
            from app.rag.embeddings import get_embedder
            st.embedder = get_embedder()
        if st.searcher is None:
            from app.rag.index import HybridSearcher
            reranker = None
            if s.reranker_model:
                from sentence_transformers import CrossEncoder
                reranker = CrossEncoder(s.reranker_model)
            st.searcher = HybridSearcher(st.embedder, reranker)
        _wire(st)
        stop = asyncio.Event()
        task = None
        if s.scheduler_in_api:
            from app.services.tasks import scheduler_loop_all
            task = asyncio.create_task(scheduler_loop_all(st.sessionmaker, st.publisher, st.task_deps,
                                                          interval=s.scheduler_interval, stop=stop))
        if s.api_dev_user_id is not None:
            log.warning("API_DEV_USER_ID задан: запросы без initData авторизуются как %s. Только для локальной "
                        "разработки!", s.api_dev_user_id)
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="Glass Agent API", version="0.3.0", lifespan=lifespan,
                  description="Backend AI-маркетолога. Авторизация: `Authorization: tma <initData>`. "
                              "Ошибки: `{\"error\": {\"code\", \"message\", \"reason\"}}`. См. docs/FRONTEND.md")
    st: Any = app.state
    st.sessionmaker, st.connector, st.embedder, st.searcher = sessionmaker, connector, embedder, searcher
    st.notifier, st.publisher, st.orchestrator, st.login_session = notifier, publisher, None, None
    st.task_deps = None
    st.bot_token = bot_token if bot_token is not None else (
        s.tg_bot_token.get_secret_value() if s.tg_bot_token else None)
    if connector is not None and searcher is not None:
        _wire(st)
    origins = cors_origins or s.cors_origin_list
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                           allow_headers=["*"], allow_credentials=False)

    # единый формат ошибок для фронтенда
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
                413: "TOO_LARGE", 422: "VALIDATION_ERROR"}.get(exc.status_code, f"HTTP_{exc.status_code}")
        return JSONResponse(status_code=exc.status_code, headers=getattr(exc, "headers", None),
                            content={"error": {"code": code, "message": str(exc.detail), "reason": None}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": "Некорректные данные запроса", "reason": None,
            "fields": [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]}})

    app.include_router(router)

    @app.get("/health", tags=["system"])
    async def health():
        return {"ok": True}

    return app


app = create_app()
