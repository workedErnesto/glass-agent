"""Секреты компаний (токены соцсетей): шифрование Fernet тем же ключом, что и токены ChatGPT."""
from __future__ import annotations

import uuid
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.auth.token_storage import load_fernet
from app.core.config import get_settings
from app.db.models import AuditLog, CompanySecret

ALLOWED = {
    "vk_token": "ключ пользователя-администратора сообщества VK (права wall, photos, offline) — публикация и ответы",
    "max_bot_token": "токен бота MAX компании (если отличается от общего MAX_BOT_TOKEN)",
    "telegram_bot_token": "токен отдельного бота Telegram компании (если отличается от общего TG_BOT_TOKEN)",
}


@lru_cache
def _fernet() -> Fernet:
    s = get_settings()
    return load_fernet(s.glass_secret_key, s.master_key_path)


async def set_secret(session: AsyncSession, company_id: uuid.UUID, name: str, value: str,
                     user_id: int | None = None) -> None:
    if name not in ALLOWED:
        raise ValueError(f"Неизвестный секрет {name}; допустимо: {', '.join(ALLOWED)}")
    value = value.strip()
    if not value:
        raise ValueError("Пустое значение")
    row = await session.get(CompanySecret, (company_id, name))
    enc = _fernet().encrypt(value.encode()).decode()
    if row is None:
        session.add(CompanySecret(company_id=company_id, name=name, value_encrypted=enc, hint=value[-4:]))
    else:
        row.value_encrypted, row.hint = enc, value[-4:]
    session.add(AuditLog(company_id=company_id, user_id=user_id, action="secret.set", details={"name": name}))


async def get_secret(session: AsyncSession, company_id: uuid.UUID, name: str) -> str | None:
    row = await session.get(CompanySecret, (company_id, name))
    return _fernet().decrypt(row.value_encrypted.encode()).decode() if row else None


async def delete_secret(session: AsyncSession, company_id: uuid.UUID, name: str, user_id: int | None = None) -> bool:
    row = await session.get(CompanySecret, (company_id, name))
    if row is None:
        return False
    await session.delete(row)
    session.add(AuditLog(company_id=company_id, user_id=user_id, action="secret.delete", details={"name": name}))
    return True


async def list_secrets(session: AsyncSession, company_id: uuid.UUID) -> list[dict]:
    rows = (await session.scalars(select(CompanySecret).where(CompanySecret.company_id == company_id))).all()
    return [{"name": r.name, "hint": "…" + r.hint, "description": ALLOWED.get(r.name, ""),
             "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows]
