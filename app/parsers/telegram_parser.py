"""Парсер Telegram-каналов.

* `TelethonChannelParser` — доступная история канала через Telethon (MTProto) от
  имени авторизованного пользовательского аккаунта, который состоит в канале
  (или получает доступ по приглашению). Ограничения доступа Telegram не обходятся.
* `TelegramBotChannelReader` — новые посты собственных подключённых каналов
  через Telegram Bot API (бот должен быть администратором канала).

Собираются: текст, даты, просмотры, пересылки, реакции, комментарии,
хештеги, типы медиа, ссылки на посты. Синхронизация инкрементальная
(по последнему id), дубли исключаются по ключу канал+пост.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from pydantic import SecretStr

from .base import (
    IndexCallback,
    JsonlDocumentStore,
    ParsedDocument,
    SourceType,
    SyncResult,
    UpsertStatus,
    clean_text,
    extract_hashtags,
    make_title,
    utcnow,
)

log = logging.getLogger(__name__)

INVITE_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", re.I)
PUBLIC_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][\w]{3,})", re.I)


class TelegramConfigError(RuntimeError):
    """Нет api_id/api_hash или сессия не авторизована."""


class TelegramAccessError(RuntimeError):
    """Нет доступа к каналу (не участник, приглашение недействительно и т.п.)."""


def parse_channel_ref(ref: str) -> tuple[str, str]:
    """Возвращает ("invite", hash) | ("username", name) | ("id", "-100...")."""
    ref = ref.strip()
    if m := INVITE_RE.match(ref):
        return "invite", m.group(1)
    if ref.startswith("+") and len(ref) > 10:
        return "invite", ref[1:]
    if m := PUBLIC_RE.match(ref):
        return "username", m.group(1)
    if ref.lstrip("-").isdigit():
        return "id", ref
    return "username", ref.lstrip("@")


# ----------------------------------------------------------------------------- Telethon


def _media_type(msg: Any) -> str | None:
    if getattr(msg, "media", None) is None:
        return None
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "video", None) or getattr(msg, "gif", None):
        return "video" if getattr(msg, "video", None) else "gif"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "poll", None):
        return "poll"
    if getattr(msg, "web_preview", None):
        return "link_preview"
    if getattr(msg, "document", None):
        return "document"
    return type(msg.media).__name__.removeprefix("MessageMedia").lower()


def _reactions(msg: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    reactions = getattr(msg, "reactions", None)
    for rc in getattr(reactions, "results", None) or []:
        r = rc.reaction
        if hasattr(r, "emoticon"):
            key = r.emoticon
        elif hasattr(r, "document_id"):
            key = f"custom:{r.document_id}"
        else:
            key = type(r).__name__.removeprefix("Reaction").lower() or "other"
        out[key] = out.get(key, 0) + int(rc.count)
    return out


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def messages_to_documents(messages: Iterable[Any], *, channel_id: int, channel_title: str,
                          username: str | None) -> list[ParsedDocument]:
    """Преобразует сообщения Telethon в документы. Альбомы (grouped_id) склеиваются в один пост."""
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    for m in messages:
        if getattr(m, "action", None) is not None:  # служебные сообщения (закреп, смена фото...)
            continue
        key = f"album{m.grouped_id}" if getattr(m, "grouped_id", None) else str(m.id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    docs: list[ParsedDocument] = []
    for key in order:
        parts = sorted(groups[key], key=lambda x: x.id)
        first = parts[0]
        text = clean_text("\n\n".join(p.message for p in parts if getattr(p, "message", None)))
        media = [t for t in (_media_type(p) for p in parts) if t]
        if not text and not media:
            continue
        link = (f"https://t.me/{username}/{first.id}" if username
                else f"https://t.me/c/{channel_id}/{first.id}")
        reactions: dict[str, int] = {}
        for p in parts:
            for k, v in _reactions(p).items():
                reactions[k] = max(reactions.get(k, 0), v)
        replies = max((getattr(getattr(p, "replies", None), "replies", 0) or 0) for p in parts)
        fwd = getattr(first, "fwd_from", None)
        metrics = {
            "views": max((getattr(p, "views", None) or 0) for p in parts),
            "forwards": max((getattr(p, "forwards", None) or 0) for p in parts),
            "replies": replies,
            "reactions": reactions,
            "reactions_total": sum(reactions.values()),
        }
        docs.append(ParsedDocument(
            source_type=SourceType.TELEGRAM,
            source_id=str(channel_id),
            external_id=key,
            url=link,
            title=make_title(text, media),
            text=text,
            published_at=_as_utc(first.date),
            edited_at=_as_utc(max((p.edit_date for p in parts if getattr(p, "edit_date", None)), default=None)),
            metrics=metrics,
            metadata={
                "channel_title": channel_title,
                "channel_username": username,
                "message_ids": [p.id for p in parts],
                "hashtags": extract_hashtags(text),
                "media_types": media,
                "media_count": len(media),
                "is_forward": fwd is not None,
                "post_author": getattr(first, "post_author", None),
                "pinned": bool(getattr(first, "pinned", False)),
            },
        ))
    return docs


class TelethonChannelParser:
    def __init__(self, api_id: int | None, api_hash: SecretStr | str | None, session_path: Path | str):
        if not api_id or not api_hash:
            raise TelegramConfigError(
                "Не заданы TG_API_ID / TG_API_HASH. Получите их на https://my.telegram.org "
                "(API development tools) и авторизуйте сессию: python -m scripts.telegram_login"
            )
        self.api_id = int(api_id)
        self.api_hash = api_hash.get_secret_value() if isinstance(api_hash, SecretStr) else api_hash
        self.session_path = Path(session_path)

    def client(self):
        from telethon import TelegramClient

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(str(self.session_path), self.api_id, self.api_hash)

    async def resolve(self, client, ref: str, *, join: bool = False):
        from telethon import errors
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        from telethon.tl.types import ChatInviteAlready, ChatInvitePeek

        kind, value = parse_channel_ref(ref)
        if kind != "invite":
            try:
                return await client.get_entity(int(value) if kind == "id" else value)
            except (ValueError, errors.RPCError) as e:
                raise TelegramAccessError(f"Канал {ref} не найден или недоступен: {e}") from e
        try:
            invite = await client(CheckChatInviteRequest(value))
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError) as e:
            raise TelegramAccessError(f"Приглашение недействительно или истекло: {ref}") from e
        if isinstance(invite, (ChatInviteAlready, ChatInvitePeek)):
            return invite.chat
        if not join:
            raise TelegramAccessError(
                f"Аккаунт не состоит в приватном канале «{getattr(invite, 'title', '?')}». "
                "Вступите в канал из приложения Telegram или запустите парсер с флагом --join."
            )
        try:
            updates = await client(ImportChatInviteRequest(value))
        except errors.InviteRequestSentError as e:
            raise TelegramAccessError("Отправлена заявка на вступление — дождитесь одобрения администратора.") from e
        return updates.chats[0]

    async def sync(self, ref: str, store: JsonlDocumentStore, *, limit: int | None = None, join: bool = False,
                   refresh_recent: int = 50, index_callback: IndexCallback | None = None) -> SyncResult:
        client = self.client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramConfigError(
                    f"Сессия {self.session_path} не авторизована. Выполните: python -m scripts.telegram_login"
                )
            entity = await self.resolve(client, ref, join=join)
            channel_id = entity.id
            username = getattr(entity, "username", None)
            title = getattr(entity, "title", str(channel_id))
            result = SyncResult(source_type=SourceType.TELEGRAM, source_id=str(channel_id))
            state_key = f"telegram:{channel_id}"
            state = store.get_state(state_key)
            last_id = int(state.get("last_message_id", 0))

            # 1) новые сообщения (инкрементально, от старых к новым)
            new_msgs = [m async for m in client.iter_messages(entity, min_id=last_id, reverse=True, limit=limit)]
            # 2) обновление показателей/правок последних постов
            recent = []
            if last_id and refresh_recent:
                recent = [m async for m in client.iter_messages(entity, max_id=last_id + 1, limit=refresh_recent)]

            changed: list[ParsedDocument] = []
            seen: set[str] = set()
            for doc in messages_to_documents(recent + new_msgs, channel_id=channel_id, channel_title=title,
                                             username=username):
                if doc.key in seen:
                    continue
                seen.add(doc.key)
                status = store.upsert(doc)
                result.count(status)
                if status in (UpsertStatus.NEW, UpsertStatus.UPDATED):
                    changed.append(doc)

            max_id = max([last_id] + [m.id for m in new_msgs])
            full = await self._channel_info(client, entity)
            store.set_state(state_key, {
                "ref": ref, "title": title, "username": username, "last_message_id": max_id,
                "last_sync": utcnow().isoformat(), **full,
            })
            result.notes.append(f"канал «{title}» (id {channel_id}); новых сообщений: {len(new_msgs)}, "
                                f"последний id: {max_id}")
            store.save()
            if index_callback and changed:
                await index_callback(changed)
            result.finished_at = utcnow()
            return result
        finally:
            await client.disconnect()

    @staticmethod
    async def _channel_info(client, entity) -> dict[str, Any]:
        from telethon import errors
        from telethon.tl.functions.channels import GetFullChannelRequest

        try:
            full = await client(GetFullChannelRequest(entity))
            return {"about": full.full_chat.about, "participants_count": full.full_chat.participants_count}
        except errors.RPCError as e:
            return {"info_error": str(e)}


# ----------------------------------------------------------------------------- Bot API

BOT_MEDIA_KEYS = ("photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker", "poll")


def bot_post_to_document(post: dict[str, Any]) -> ParsedDocument | None:
    chat = post.get("chat", {})
    text = clean_text(post.get("text") or post.get("caption") or "")
    media = [k for k in BOT_MEDIA_KEYS if k in post]
    if not text and not media:
        return None
    username = chat.get("username")
    chat_id = int(chat["id"])
    short_id = str(chat_id)[4:] if str(chat_id).startswith("-100") else str(abs(chat_id))
    mid = post["message_id"]
    entities = post.get("entities") or post.get("caption_entities") or []
    raw = post.get("text") or post.get("caption") or ""
    tags = [raw[e["offset"]:e["offset"] + e["length"]].lstrip("#").lower() for e in entities if e.get("type") == "hashtag"]
    key = f"album{post['media_group_id']}" if post.get("media_group_id") else str(mid)
    return ParsedDocument(
        source_type=SourceType.TELEGRAM,
        source_id=short_id,
        external_id=key,
        url=f"https://t.me/{username}/{mid}" if username else f"https://t.me/c/{short_id}/{mid}",
        title=make_title(text, media),
        text=text,
        published_at=datetime.fromtimestamp(post["date"], tz=timezone.utc),
        edited_at=datetime.fromtimestamp(post["edit_date"], tz=timezone.utc) if post.get("edit_date") else None,
        metrics={},  # Bot API не отдаёт просмотры и реакции постов канала
        metadata={
            "channel_title": chat.get("title"),
            "channel_username": username,
            "message_ids": [mid],
            "hashtags": tags or extract_hashtags(text),
            "media_types": media,
            "media_count": len(media),
            "is_forward": "forward_origin" in post or "forward_from_chat" in post,
            "via": "bot_api",
        },
    )


class TelegramBotChannelReader:
    """Чтение новых постов каналов, где бот — администратор (getUpdates).

    Важно: getUpdates нельзя использовать одновременно с webhook и с другим
    процессом, читающим обновления того же бота. В продакшене эти обновления
    должен получать основной обработчик бота и передавать их сюда через
    `bot_post_to_document`.
    """

    API = "https://api.telegram.org"

    def __init__(self, token: SecretStr | str | None, transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise TelegramConfigError("Не задан TG_BOT_TOKEN")
        self._token = token.get_secret_value() if isinstance(token, SecretStr) else token
        self._transport = transport

    async def sync(self, store: JsonlDocumentStore, index_callback: IndexCallback | None = None) -> SyncResult:
        result = SyncResult(source_type=SourceType.TELEGRAM, source_id="bot_api")
        state = store.get_state("telegram_bot")
        offset = int(state.get("offset", 0))
        changed: list[ParsedDocument] = []
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            while True:
                r = await client.post(f"{self.API}/bot{self._token}/getUpdates", json={
                    "offset": offset, "timeout": 0, "limit": 100,
                    "allowed_updates": ["channel_post", "edited_channel_post"],
                })
                data = r.json()
                if not data.get("ok"):
                    raise TelegramAccessError(f"Bot API: {data.get('error_code')} {data.get('description')}")
                updates = data["result"]
                for upd in updates:
                    offset = max(offset, upd["update_id"] + 1)
                    post = upd.get("channel_post") or upd.get("edited_channel_post")
                    doc = bot_post_to_document(post) if post else None
                    if doc is None:
                        result.skipped += 1
                        continue
                    old = store.get(doc.key)
                    if old is not None and doc.external_id.startswith("album"):
                        # часть альбома: дополняем уже сохранённый пост
                        doc.text = clean_text("\n\n".join(x for x in (old.text, doc.text) if x))
                        doc.metadata["message_ids"] = sorted(set(old.metadata["message_ids"]) | set(doc.metadata["message_ids"]))
                        doc.metadata["media_types"] = old.metadata["media_types"] + doc.metadata["media_types"]
                        doc.metadata["media_count"] = len(doc.metadata["media_types"])
                        doc.content_hash = ""
                        doc.model_post_init(None)
                    status = store.upsert(doc)
                    result.count(status)
                    if status in (UpsertStatus.NEW, UpsertStatus.UPDATED):
                        changed.append(doc)
                if len(updates) < 100:
                    break
        store.set_state("telegram_bot", {"offset": offset, "last_sync": utcnow().isoformat()})
        store.save()
        if index_callback and changed:
            await index_callback(changed)
        result.finished_at = utcnow()
        return result
