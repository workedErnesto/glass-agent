"""Жизненный цикл публикаций и согласование.

draft → pending_approval → approved → scheduled → published
pending_approval → rejected → draft (доработка)
Любое изменение текста/изображения после согласования возвращает пост в draft —
нужно повторное согласование. Публикация без согласования невозможна:
перед отправкой сверяется approved_hash с текущим содержимым.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Post

from . import memory as mem

DRAFT, PENDING, APPROVED, SCHEDULED, PUBLISHING, PUBLISHED, REJECTED, FAILED, REVISION = (
    "draft", "pending_approval", "approved", "scheduled", "publishing", "published", "rejected", "failed", "revision")
EDITABLE = {DRAFT, PENDING, APPROVED, SCHEDULED, REJECTED, FAILED, REVISION}
STATUSES = (DRAFT, PENDING, REVISION, APPROVED, SCHEDULED, PUBLISHING, PUBLISHED, REJECTED, FAILED)
PLATFORMS = ("telegram", "vk", "max")

TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {PENDING},
    PENDING: {APPROVED, REJECTED, REVISION, DRAFT},
    REVISION: {DRAFT, PENDING},  # возврат на доработку с комментарием
    REJECTED: {DRAFT, PENDING},
    APPROVED: {SCHEDULED, DRAFT},
    SCHEDULED: {PUBLISHING, APPROVED, DRAFT},
    PUBLISHING: {PUBLISHED, SCHEDULED, FAILED},
    FAILED: {SCHEDULED, DRAFT},
    PUBLISHED: set(),
}


class PostStateError(RuntimeError):
    pass


def content_digest(post: Post) -> str:
    h = hashlib.sha256()
    h.update((post.platform or "").encode() + b"\x00")  # согласование действует для конкретной площадки
    h.update((post.text or "").strip().encode())
    h.update(b"\x00")
    if post.image_path:
        try:
            with open(post.image_path, "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
        except OSError:
            h.update(post.image_path.encode())
    return h.hexdigest()


def is_approved_content(post: Post) -> bool:
    return bool(post.approved_hash) and post.approved_hash == content_digest(post)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _move(post: Post, new: str) -> None:
    if new not in TRANSITIONS.get(post.status, set()):
        raise PostStateError(f"Нельзя перевести пост из {post.status} в {new}")
    post.status = new


def _audit(session: AsyncSession, post: Post, action: str, user_id: int | None, **details: Any) -> None:
    session.add(AuditLog(company_id=post.company_id, user_id=user_id, action=f"post.{action}",
                         details={"post_id": str(post.id), "status": post.status, **details}))


async def get_post(session: AsyncSession, company_id: uuid.UUID, post_id: uuid.UUID | str,
                   *, for_update: bool = False) -> Post:
    try:
        pid = post_id if isinstance(post_id, uuid.UUID) else uuid.UUID(str(post_id))
    except ValueError as e:
        raise LookupError("Пост не найден") from e
    q = select(Post).where(Post.id == pid, Post.company_id == company_id)
    if for_update:
        q = q.with_for_update()
    post = await session.scalar(q)
    if post is None:
        raise LookupError("Пост не найден")
    return post


async def list_posts(session: AsyncSession, company_id: uuid.UUID, *, status: str | None = None,
                     limit: int = 50) -> list[Post]:
    q = select(Post).where(Post.company_id == company_id)
    if status:
        q = q.where(Post.status == status)
    return list((await session.scalars(q.order_by(Post.created_at.desc()).limit(limit))).all())


async def create_draft(session: AsyncSession, company_id: uuid.UUID, *, text: str, topic: str = "",
                       user_id: int | None = None, generated_by: dict[str, Any] | None = None,
                       platform: str = "telegram", proposed_at: datetime | None = None) -> Post:
    if platform not in PLATFORMS:
        raise PostStateError(f"Площадка {platform} не поддерживается: {', '.join(PLATFORMS)}")
    post = Post(company_id=company_id, status=DRAFT, topic=topic, text=text.strip(), platform=platform,
                created_by_user_id=user_id, generated_by=generated_by or {}, revision=1, meta={}, image_meta={},
                proposed_at=_aware(proposed_at))
    session.add(post)
    await session.flush()
    _audit(session, post, "create", user_id, topic=topic)
    await mem.remember(session, company_id, f"Создан черновик поста «{topic or text[:60]}» (id {post.id})",
                       kind="episodic", category="post_created", meta={"post_id": str(post.id)})
    return post


async def edit_content(session: AsyncSession, post: Post, *, text: str | None = None,
                       image_path: str | None = None, image_meta: dict[str, Any] | None = None,
                       platform: str | None = None, user_id: int | None = None) -> Post:
    """Изменение контента. Если пост был согласован/запланирован — согласование сбрасывается."""
    if post.status not in EDITABLE:
        raise PostStateError(f"Пост в статусе {post.status} изменять нельзя")
    before = content_digest(post)
    if text is not None:
        post.text = text.strip()
    if image_path is not None:
        post.image_path = image_path
        post.image_meta = image_meta or {}
    if platform is not None:
        if platform not in PLATFORMS:
            raise PostStateError(f"Площадка {platform} не поддерживается")
        post.platform = platform
    if content_digest(post) == before:
        return post
    post.revision += 1
    if post.status != DRAFT:
        post.status = DRAFT  # контент изменился — требуется повторное согласование
    post.approved_hash = None
    post.approved_at = None
    post.approved_by_user_id = None
    post.scheduled_at = None
    _audit(session, post, "edit", user_id, revision=post.revision)
    await mem.remember(session, post.company_id, f"Пост {post.id} изменён (ревизия {post.revision}), "
                       "требуется повторное согласование", kind="episodic", category="post_updated",
                       meta={"post_id": str(post.id)})
    return post


async def run_checks(session: AsyncSession, post: Post) -> dict[str, Any]:
    from app.db.models import Company

    from .checks import check_post

    company = await session.get(Company, post.company_id)
    recent = (await session.scalars(select(Post.text).where(
        Post.company_id == post.company_id, Post.status == PUBLISHED, Post.id != post.id)
        .order_by(Post.published_at.desc()).limit(20))).all()
    result = check_post(post, company, list(recent))
    post.meta = {**(post.meta or {}), "checks": result}
    return result


async def submit(session: AsyncSession, post: Post, *, user_id: int | None = None,
                 proposed_at: datetime | None = None) -> Post:
    result = await run_checks(session, post)
    if not result["ok"]:
        raise PostStateError("Проверка не пройдена: " + "; ".join(result["errors"]))
    if proposed_at is not None:
        post.proposed_at = _aware(proposed_at)
    _move(post, PENDING)
    _audit(session, post, "submit", user_id, warnings=result["warnings"])
    return post


async def request_changes(session: AsyncSession, post: Post, *, user_id: int | None, comment: str) -> Post:
    """Возврат на доработку с комментарием (п. 3.5 ТЗ)."""
    if not comment.strip():
        raise PostStateError("Нужен комментарий: что доработать")
    _move(post, REVISION)
    post.review_comment = comment.strip()
    post.approved_hash = None
    _audit(session, post, "request_changes", user_id, comment=comment)
    await mem.remember(session, post.company_id, f"Пост {post.id} возвращён на доработку: {comment}",
                       kind="episodic", category="post_rejected", meta={"post_id": str(post.id)})
    return post


async def approve(session: AsyncSession, post: Post, *, user_id: int | None,
                  scheduled_at: datetime | None = None, target_chat_id: str | None = None) -> Post:
    if post.status in (DRAFT, REJECTED, REVISION):
        await submit(session, post, user_id=user_id)  # одобрение из черновика = отправка + согласование
    _move(post, APPROVED)
    post.approved_by_user_id = user_id
    post.approved_at = _now()
    post.approved_hash = content_digest(post)
    post.review_comment = None
    if target_chat_id:
        post.target_chat_id = target_chat_id
    _audit(session, post, "approve", user_id)
    await mem.remember(session, post.company_id, f"Пост {post.id} согласован", kind="episodic",
                       category="post_approved", meta={"post_id": str(post.id)})
    if scheduled_at is not None:
        await schedule(session, post, scheduled_at, user_id=user_id)
    return post


async def reject(session: AsyncSession, post: Post, *, user_id: int | None, comment: str = "") -> Post:
    _move(post, REJECTED)
    post.review_comment = comment or None
    post.approved_hash = None
    _audit(session, post, "reject", user_id, comment=comment)
    await mem.remember(session, post.company_id, f"Пост {post.id} отклонён: {comment or 'без комментария'}",
                       kind="episodic", category="post_rejected", meta={"post_id": str(post.id)})
    return post


async def schedule(session: AsyncSession, post: Post, at: datetime | None, *, user_id: int | None = None) -> Post:
    if not is_approved_content(post):
        raise PostStateError("Контент не согласован или изменён после согласования")
    at = at or _now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    _move(post, SCHEDULED)
    post.scheduled_at = at
    _audit(session, post, "schedule", user_id, at=at.isoformat())
    await mem.remember(session, post.company_id, f"Пост {post.id} запланирован на {at.isoformat()}",
                       kind="episodic", category="post_scheduled", meta={"post_id": str(post.id)})
    return post


async def claim_for_publishing(session: AsyncSession, post_id: uuid.UUID) -> bool:
    """Атомарно забирает пост в публикацию — защита от двойной отправки параллельными воркерами."""
    res = await session.execute(update(Post).where(Post.id == post_id, Post.status == SCHEDULED,
                                                   Post.external_message_id.is_(None))
                                .values(status=PUBLISHING, publish_attempts=Post.publish_attempts + 1))
    return res.rowcount == 1


def post_to_dict(p: Post) -> dict[str, Any]:
    return {
        "id": str(p.id), "status": p.status, "platform": p.platform, "topic": p.topic, "text": p.text,
        "proposed_at": p.proposed_at.isoformat() if p.proposed_at else None,
        "checks": (p.meta or {}).get("checks"),
        # путь на сервере наружу не отдаём: картинка — GET /api/companies/{id}/posts/{post_id}/image
        "has_image": bool(p.image_path),
        "image_url": f"/api/companies/{p.company_id}/posts/{p.id}/image" if p.image_path else None,
        "image_meta": p.image_meta, "revision": p.revision,
        "generated_by": p.generated_by, "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "content_approved": is_approved_content(p), "review_comment": p.review_comment,
        "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "external_message_id": p.external_message_id, "external_url": p.external_url,
        "publish_attempts": p.publish_attempts, "last_error": p.last_error,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
