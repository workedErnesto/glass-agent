"""Схемы ответов API — контракт для фронтенда (попадают в /openapi.json)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ErrorBody(Out):
    code: str
    message: str
    reason: str | None = None
    fields: list[dict[str, Any]] | None = None


class ErrorOut(Out):
    error: ErrorBody


class MeOut(Out):
    telegram_id: int | None
    name: str
    is_admin: bool


class CompanyOut(Out):
    id: str
    slug: str
    name: str
    profile: dict[str, Any]
    settings: dict[str, Any]
    role: str | None = None
    created_at: str | None = None


class MemberOut(Out):
    telegram_id: int | None
    name: str = ""
    role: str


class SecretOut(Out):
    name: str
    hint: str
    description: str = ""
    updated_at: str | None = None


class SourceOut(Out):
    id: str
    type: str
    ref: str
    name: str
    is_competitor: bool
    status: str
    options: dict[str, Any]
    last_error: str | None = None
    last_sync_at: str | None = None
    last_result: dict[str, Any] = {}


class MessageOut(Out):
    id: int
    role: str
    content: str
    provider: str | None = None
    model: str | None = None
    is_mock: bool
    meta: dict[str, Any]
    created_at: str | None = None


class ChatReplyOut(Out):
    message_id: int
    user_message_id: int
    answer: str
    provider: str
    model: str
    is_mock: bool
    steps: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    remembered: list[str]
    created_post_ids: list[str]
    llm_calls: int
    stopped_reason: str | None = None
    summary_created: bool = False


class MemoryOut(Out):
    current_task: str | None = None
    long_term: list[dict[str, Any]]
    episodic: list[dict[str, Any]]
    summaries: list[dict[str, Any]]


class SearchHitOut(Out):
    chunk_id: int
    document_id: str
    text: str
    title: str
    url: str
    source_type: str
    is_competitor: bool
    published_at: str | None = None
    score: float
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None
    citation: str


class PostOut(Out):
    id: str
    status: str
    platform: str
    topic: str
    text: str
    proposed_at: str | None = None
    checks: dict[str, Any] | None = None
    has_image: bool = False
    image_url: str | None = None  # нужен заголовок Authorization: tma …
    image_meta: dict[str, Any] = {}
    revision: int
    generated_by: dict[str, Any] = {}
    approved_at: str | None = None
    content_approved: bool
    review_comment: str | None = None
    scheduled_at: str | None = None
    published_at: str | None = None
    external_message_id: str | None = None
    external_url: str | None = None
    publish_attempts: int
    last_error: str | None = None
    created_at: str | None = None


class TaskOut(Out):
    id: str
    kind: str
    title: str
    params: dict[str, Any]
    schedule: dict[str, Any]
    enabled: bool
    notify: bool
    run_count: int
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_result: dict[str, Any] = {}
    running: bool


class InboxItemOut(Out):
    id: str
    platform: str
    chat_ref: str
    external_id: str
    post_ref: str | None = None
    author_name: str
    text: str
    status: str
    received_at: str | None = None
    reply_text: str | None = None
    reply_meta: dict[str, Any] = {}
    replied_at: str | None = None


class UsageOut(Out):
    company_id: str | None
    company: str | None
    provider: str
    calls: int
    input_tokens: int
    output_tokens: int
    days: int


class AuditOut(Out):
    id: int
    action: str
    user_id: int | None
    details: dict[str, Any]
    created_at: str | None
