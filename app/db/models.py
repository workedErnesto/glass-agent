"""Модели PostgreSQL. Все данные компании привязаны к company_id — изоляция арендаторов."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Boolean, Computed, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384  # intfloat/multilingual-e5-small


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


# --------------------------------------------------------------------------- компании и доступ


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    prefs: Mapped[dict[str, Any]] = mapped_column(default=dict)  # выбранная в боте компания, ожидаемый ввод и т.п.


class Company(TimestampMixin, Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    # профиль: описание, аудитория, стиль бренда, цели, ограничения (заполняется админом/агентом)
    profile: Mapped[dict[str, Any]] = mapped_column(default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)  # правила согласования, площадки…


class CompanyMember(Base):
    __tablename__ = "company_members"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), default="owner")  # owner | approver | editor | viewer


# --------------------------------------------------------------------------- диалог и память


class Conversation(TimestampMixin, Base):
    """Один постоянный AI-диалог на компанию (история — в нашей БД, не в ChatGPT)."""
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True)
    # short-term: текущая задача диалога и прочее рабочее состояние
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    provider: Mapped[str | None] = mapped_column(String(50))  # chatgpt_plus | mock
    model: Mapped[str | None] = mapped_column(String(100))
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_messages_conv_id", "conversation_id", "id"),)


class Memory(TimestampMixin, Base):
    """Долговременная (long_term) и эпизодическая (episodic) память компании."""
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # long_term | episodic
    category: Mapped[str] = mapped_column(String(50), default="fact")  # brand_style, goal, preference, decision…
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(Integer, default=3)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    from_message_id: Mapped[int] = mapped_column(BigInteger)
    to_message_id: Mapped[int] = mapped_column(BigInteger)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- источники и знания


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20))  # website | telegram | vk | file
    ref: Mapped[str] = mapped_column(String(1000))  # URL / ссылка на канал / группа / имя файла
    name: Mapped[str] = mapped_column(String(300), default="")
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)  # max_pages, lang…
    status: Mapped[str] = mapped_column(String(20), default="new")  # new | syncing | ok | error
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    sync_state: Mapped[dict[str, Any]] = mapped_column(default=dict)  # курсоры инкрементальной синхронизации
    last_result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (UniqueConstraint("company_id", "type", "ref"),)

    documents: Mapped[list["Document"]] = relationship(back_populates="source", cascade="all, delete-orphan",
                                                       passive_deletes=True)


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(20))
    external_key: Mapped[str] = mapped_column(String(1200))  # ParsedDocument.key
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    indexed_hash: Mapped[str | None] = mapped_column(String(64))  # hash, по которому построены чанки
    __table_args__ = (UniqueConstraint("source_id", "external_key"),)

    source: Mapped[Source] = relationship(back_populates="documents")


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('russian', text)", persisted=True))
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        Index("ix_chunks_company", "company_id", "is_competitor"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_embedding", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64}, postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class Post(TimestampMixin, Base):
    """Публикация: draft → pending_approval → approved → scheduled → published (+ rejected, failed).

    approved_hash — хеш текста и изображения на момент согласования: если контент
    изменился, публикация невозможна без повторного согласования."""
    __tablename__ = "posts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    platform: Mapped[str] = mapped_column(String(20), default="telegram")  # telegram | vk | max
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # предлагаемое время публикации
    topic: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str | None] = mapped_column(String(1000))
    image_meta: Mapped[dict[str, Any]] = mapped_column(default=dict)  # как получено изображение (шаблон/модель)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    generated_by: Mapped[dict[str, Any]] = mapped_column(default=dict)  # provider/model/is_mock
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    review_comment: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_chat_id: Mapped[str | None] = mapped_column(String(100))
    external_message_id: Mapped[str | None] = mapped_column(String(100))
    external_url: Mapped[str | None] = mapped_column(String(1000))
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ScheduledTask(TimestampMixin, Base):
    """Регулярная задача компании, выполняемая без участия пользователя.

    kind: report | sync_sources | collect_results | collect_comments | agent
    schedule: {"every_minutes": 360} или {"daily_at": "09:00", "weekdays": [0], "tz": "Europe/Moscow"}"""
    __tablename__ = "scheduled_tasks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300), default="")
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    schedule: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))  # ok | error
    last_result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class InboxItem(TimestampMixin, Base):
    """Комментарий или обращение из соцсети и ответ на него.

    status: new | draft_ready | approved | replied | ignored | error.
    Автоматическая отправка ответа — только при settings.permissions.auto_reply компании."""
    __tablename__ = "inbox_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(20))  # telegram | vk | max
    chat_ref: Mapped[str] = mapped_column(String(200))  # группа обсуждения / owner_id стены
    external_id: Mapped[str] = mapped_column(String(100))  # id сообщения / комментария
    post_ref: Mapped[str | None] = mapped_column(String(200))  # к какому посту
    author_id: Mapped[str | None] = mapped_column(String(100))
    author_name: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    reply_text: Mapped[str | None] = mapped_column(Text)
    reply_meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reply_external_id: Mapped[str | None] = mapped_column(String(100))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (UniqueConstraint("company_id", "platform", "chat_ref", "external_id"),)


class CompanySecret(TimestampMixin, Base):
    """Токены соцсетей компании (VK, MAX…), зашифрованы Fernet. Через API значения не отдаются."""
    __tablename__ = "company_secrets"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), primary_key=True)  # vk_token | max_bot_token | …
    value_encrypted: Mapped[str] = mapped_column(Text)
    hint: Mapped[str] = mapped_column(String(20), default="")  # последние символы для узнаваемости


class AIUsage(Base):
    """Расход вызовов модели по компаниям и дням (контроль расходов и лимит на компанию)."""
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    day: Mapped[datetime] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(50))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (UniqueConstraint("company_id", "day", "provider"),)


class AuditLog(Base):
    """Журнал действий (требование безопасности ТЗ)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


