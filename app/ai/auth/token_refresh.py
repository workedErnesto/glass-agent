"""Выдача действующего access token с безопасным обновлением.

Refresh token ротируется при каждом обновлении, поэтому обновления
сериализуются: asyncio.Lock внутри процесса + файловая блокировка (fcntl)
между процессами (API, бот, планировщик). После захвата блокировки
учётные данные перечитываются — их мог уже обновить другой процесс.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from ..connectors.base import AIConnectionUnavailable
from .oauth import REAUTH_ERRORS, REQUIRED_SCOPE, OAuthError, refresh_tokens, unverified_claims
from .token_storage import ChatGPTCredentials, EncryptedTokenStorage

log = logging.getLogger(__name__)
REFRESH_MARGIN = timedelta(minutes=5)


class TokenManager:
    def __init__(self, storage: EncryptedTokenStorage,
                 client_factory: Callable[[], httpx.AsyncClient] | None = None):
        self.storage = storage
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30))
        self._lock = asyncio.Lock()

    def load(self) -> ChatGPTCredentials | None:
        return self.storage.load()

    @staticmethod
    def _needs_refresh(creds: ChatGPTCredentials) -> bool:
        now = datetime.now(timezone.utc)
        if creds.earliest_refresh_at and now < creds.earliest_refresh_at and now < creds.expires_at:
            return False  # сервер просит не обновлять раньше earliest_refresh_at, пока токен действует
        return now >= creds.expires_at - REFRESH_MARGIN

    async def get_credentials(self, *, force_refresh: bool = False) -> ChatGPTCredentials:
        creds = self.storage.load()
        if creds is None:
            raise AIConnectionUnavailable("Аккаунт ChatGPT Plus не подключён", reason="not_connected")
        if REQUIRED_SCOPE not in creds.scopes:
            raise AIConnectionUnavailable(
                f"Нет разрешения {REQUIRED_SCOPE} на использование подписки ChatGPT", reason="scope_missing")
        if not force_refresh and not self._needs_refresh(creds):
            return creds
        async with self._lock:
            # тяжёлая файловая блокировка — в отдельном потоке, чтобы не блокировать event loop
            await asyncio.to_thread(self._acquire_and_hold_marker)
            try:
                creds = self.storage.load()
                if creds is None:
                    raise AIConnectionUnavailable("Аккаунт ChatGPT Plus отключён", reason="not_connected")
                if not force_refresh and not self._needs_refresh(creds):
                    return creds
                return await self._refresh(creds)
            finally:
                self._release_marker()

    # Файловая блокировка удерживается на время refresh
    _lock_fh = None

    def _acquire_and_hold_marker(self) -> None:
        path = self.storage.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        self._lock_fh = fh

    def _release_marker(self) -> None:
        if self._lock_fh is not None:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    async def _refresh(self, creds: ChatGPTCredentials) -> ChatGPTCredentials:
        async with self._client_factory() as client:
            try:
                body = await refresh_tokens(client, client_id=creds.client_id, refresh_token=creds.refresh_token)
            except OAuthError as e:
                if e.error in REAUTH_ERRORS:
                    self.storage.clear()
                    raise AIConnectionUnavailable(
                        "Авторизация ChatGPT истекла или отозвана — войдите заново", reason="reauth_required") from e
                if e.error == "invalid_client":
                    raise AIConnectionUnavailable("Неверный client_id приложения", reason="invalid_client") from e
                raise AIConnectionUnavailable(f"Не удалось обновить токен: {e}", reason="refresh_failed") from e
            except httpx.TransportError as e:
                raise AIConnectionUnavailable(f"Сеть недоступна при обновлении токена: {e}",
                                              reason="network") from e
        claims = unverified_claims(body["access_token"])
        if "scope" not in body and claims.get("scope"):
            sc = claims["scope"]
            body["scope"] = " ".join(sc) if isinstance(sc, list) else sc
        new = ChatGPTCredentials.from_token_response(body, client_id=creds.client_id,
                                                     host_id=creds.ext_agent_host_id, previous=creds)
        self.storage.save(new)
        log.info("Токен ChatGPT обновлён, действует до %s", new.expires_at.isoformat())
        return new
