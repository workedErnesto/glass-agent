"""Защищённое хранение OAuth-токенов ChatGPT.

Токены шифруются (Fernet, AES-128-CBC + HMAC) и пишутся атомарно в файл с
правами 0600. Ключ — GLASS_SECRET_KEY или автоматически созданный
data/secrets/master.key. Токены не попадают во Frontend, логи и в открытом
виде в БД.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, SecretStr


def load_fernet(secret: SecretStr | str | None, key_path: Path) -> Fernet:
    if secret:
        raw = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        return Fernet(raw.encode())
    key_path = Path(key_path)
    if key_path.exists():
        return Fernet(key_path.read_bytes().strip())
    key_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key_path.parent, 0o700)
    key = Fernet.generate_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return Fernet(key)


class ChatGPTCredentials(BaseModel):
    """Запись учётных данных (поля по примеру из token-reference)."""

    email: str | None = None
    issuer: str = "https://auth.openai.com"
    subject: str | None = None
    client_id: str
    ext_agent_host_id: str
    id_token: str | None = None
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    scopes: list[str] = Field(default_factory=list)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    earliest_refresh_at: datetime | None = None
    plan_type: str | None = None

    @property
    def expires_at(self) -> datetime:
        return self.saved_at + timedelta(seconds=self.expires_in)

    def public_view(self) -> dict[str, Any]:
        """Безопасное представление без токенов (для API/логов)."""
        return {
            "email": self.email,
            "client_id": self.client_id[:10] + "…" if self.client_id else None,
            "scopes": self.scopes,
            "expires_at": self.expires_at.isoformat(),
            "saved_at": self.saved_at.isoformat(),
        }

    @classmethod
    def from_token_response(cls, body: dict[str, Any], *, client_id: str, host_id: str,
                            previous: "ChatGPTCredentials | None" = None,
                            claims: dict[str, Any] | None = None) -> "ChatGPTCredentials":
        scope = body.get("scope")
        scopes = sorted(scope.split()) if isinstance(scope, str) else (previous.scopes if previous else [])
        earliest = body.get("earliest_refresh_at")
        if isinstance(earliest, (int, float)):
            earliest = datetime.fromtimestamp(earliest, tz=timezone.utc)
        claims = claims or {}
        return cls(
            email=claims.get("email") or (previous.email if previous else None),
            subject=claims.get("sub") or (previous.subject if previous else None),
            client_id=client_id,
            ext_agent_host_id=host_id,
            id_token=body.get("id_token") or (previous.id_token if previous else None),
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token") or (previous.refresh_token if previous else ""),
            token_type=body.get("token_type", "Bearer"),
            expires_in=int(body.get("expires_in", 3600)),
            scopes=scopes,
            earliest_refresh_at=earliest if isinstance(earliest, datetime) else None,
        )


class EncryptedTokenStorage:
    def __init__(self, path: Path, fernet: Fernet):
        self.path = Path(path)
        self._fernet = fernet

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> ChatGPTCredentials | None:
        if not self.path.exists():
            return None
        try:
            raw = self._fernet.decrypt(self.path.read_bytes())
        except InvalidToken as e:
            raise RuntimeError("Не удалось расшифровать токены ChatGPT: изменился ключ шифрования") from e
        return ChatGPTCredentials.model_validate(json.loads(raw))

    def save(self, creds: ChatGPTCredentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._fernet.encrypt(creds.model_dump_json().encode())
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".cred", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
