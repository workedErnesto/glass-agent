"""Sign in with ChatGPT — OAuth 2.0 + PKCE для использования подписки ChatGPT Plus.

Реализовано по официальной документации https://developers.openai.com/siwc/
(token-sharing-open-source: sign-in, token-reference, errors-and-recovery):

* динамическая регистрация клиента: первый вход с client_id=dynamic_agent_client,
  agent_name_hint и ext_agent_host_id; выданный client_id (oaiapp_...) приходит
  в callback и сохраняется;
* redirect только на loopback http://127.0.0.1:<port>/auth/callback;
* PKCE S256, state и nonce на каждую попытку;
* scope: openid profile email offline_access resource.invoke chatgpt.tokens.use.direct;
* проверка подписи id_token по JWKS (iss, aud, nonce).

Никаких cookie ChatGPT, неофициальных endpoint'ов и OPENAI_API_KEY.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

AUTH_ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
IDENTITY_SCOPES = ("openid", "profile", "email")
PLAN_SCOPES = ("offline_access", "resource.invoke", "chatgpt.tokens.use.direct")
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
REDIRECT_HOST = "127.0.0.1"  # документация: не заменять на localhost
REDIRECT_PATH = "/auth/callback"

# Ошибки refresh, после которых нужна повторная авторизация (errors-and-recovery)
REAUTH_ERRORS = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                 "refresh_token_invalidated", "refresh_token_reused"}


class OAuthError(RuntimeError):
    def __init__(self, message: str, *, error: str | None = None, status: int | None = None):
        super().__init__(message)
        self.error = error
        self.status = status


class ConsentDeclined(OAuthError):
    pass


class RegistrationIncomplete(OAuthError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))  # 64 символа, допустимо по RFC 7636
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def load_or_create_host_id(path: Path) -> str:
    """Стабильный непрозрачный идентификатор хоста (urn:uuid:<UUIDv4>), один на машину/VM."""
    path = Path(path)
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = f"urn:uuid:{uuid.uuid4()}"
    path.write_text(value)
    path.chmod(0o600)
    return value


@dataclass
class AuthAttempt:
    """Параметры одной попытки входа (хранятся только в памяти процесса)."""

    client_id: str  # dynamic_agent_client при первой регистрации, иначе выданный
    redirect_uri: str
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = ""
    challenge: str = ""

    def __post_init__(self) -> None:
        if not self.verifier:
            self.verifier, self.challenge = make_pkce()

    @property
    def is_registration(self) -> bool:
        return self.client_id == DYNAMIC_CLIENT_ID


def build_authorization_url(attempt: AuthAttempt, *, host_id: str, agent_name: str,
                            login_hint: str | None = None, id_token_hint: str | None = None,
                            prompt: str | None = None) -> str:
    params: dict[str, str] = {
        "client_id": attempt.client_id,
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": attempt.redirect_uri,
        "scope": " ".join(IDENTITY_SCOPES + PLAN_SCOPES),
        "resource": RESOURCE,
        "state": attempt.state,
        "nonce": attempt.nonce,
        "code_challenge_method": "S256",
        "code_challenge": attempt.challenge,
    }
    if attempt.is_registration:
        params["agent_name_hint"] = agent_name
    if login_hint:
        params["login_hint"] = login_hint
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    if prompt:
        params["prompt"] = prompt
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


@dataclass
class CallbackResult:
    code: str
    client_id: str
    granted_scope: str | None


def parse_callback(query: str, attempt: AuthAttempt) -> CallbackResult:
    q = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
    if not secrets.compare_digest(q.get("state", ""), attempt.state):
        raise OAuthError("state не совпадает — ответ отклонён (возможная CSRF-атака)", error="state_mismatch")
    if err := q.get("error"):
        if err == "access_denied":
            raise ConsentDeclined("Пользователь отклонил доступ", error=err)
        raise OAuthError(f"Ошибка авторизации: {err} {q.get('error_description', '')}".strip(), error=err)
    code = q.get("code")
    if not code:
        raise OAuthError("В callback нет code", error="no_code")
    client_id = q.get("client_id") or ""
    if attempt.is_registration:
        if not client_id or client_id == DYNAMIC_CLIENT_ID:
            raise RegistrationIncomplete("Регистрация не завершена: callback не вернул выданный client_id")
    else:
        client_id = attempt.client_id
    return CallbackResult(code=code, client_id=client_id, granted_scope=q.get("scope"))


async def _token_request(client: httpx.AsyncClient, data: dict[str, str]) -> dict[str, Any]:
    r = await client.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            err = err.get("code") or err.get("type")
        raise OAuthError(f"Token endpoint: HTTP {r.status_code} {err or ''}".strip(), error=err, status=r.status_code)
    return body


async def exchange_code(client: httpx.AsyncClient, *, client_id: str, code: str, attempt: AuthAttempt) -> dict[str, Any]:
    return await _token_request(client, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": attempt.verifier,
        "redirect_uri": attempt.redirect_uri,
        "resource": RESOURCE,
    })


async def refresh_tokens(client: httpx.AsyncClient, *, client_id: str, refresh_token: str) -> dict[str, Any]:
    # scope не передаём — сохраняется исходный grant; refresh token ротируется
    return await _token_request(client, {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "resource": RESOURCE,
    })


async def discovery(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get(DISCOVERY_URL)
    r.raise_for_status()
    return r.json()


async def revoke_refresh_token(client: httpx.AsyncClient, *, client_id: str, refresh_token: str) -> None:
    conf = await discovery(client)
    endpoint = conf.get("revocation_endpoint")
    if not endpoint:
        raise OAuthError("В openid-configuration нет revocation_endpoint")
    for attempt in range(4):
        try:
            r = await client.post(endpoint, data={"token": refresh_token, "token_type_hint": "refresh_token",
                                                  "client_id": client_id})
            if r.status_code < 500:
                if r.status_code != 200:
                    raise OAuthError(f"Отзыв токена: HTTP {r.status_code}", status=r.status_code)
                return
        except httpx.TransportError:
            if attempt == 3:
                raise
        await asyncio.sleep(2 ** attempt)
    raise OAuthError("Отзыв токена: сервер недоступен")


async def validate_id_token(client: httpx.AsyncClient, id_token: str, *, client_id: str,
                            nonce: str | None) -> dict[str, Any]:
    """Проверяет подпись (JWKS), iss, aud, exp и nonce id_token."""
    conf = await discovery(client)
    jwks = (await client.get(conf["jwks_uri"])).json()
    header = jwt.get_unverified_header(id_token)
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise OAuthError("Ключ подписи id_token не найден в JWKS")
    public_key = jwt.PyJWK(key).key
    claims = jwt.decode(id_token, public_key, algorithms=[header.get("alg", "RS256")], audience=client_id,
                        issuer=conf.get("issuer", AUTH_ISSUER), options={"require": ["exp", "iss", "aud", "sub"]})
    if nonce is not None and claims.get("nonce") != nonce:
        raise OAuthError("nonce в id_token не совпадает")
    return claims


def unverified_claims(token: str) -> dict[str, Any]:
    """Только для отображения (срок, scope). Для доверия к личности используйте validate_id_token."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}


class LoopbackCallbackServer:
    """Минимальный HTTP-сервер на 127.0.0.1 для приёма OAuth redirect.

    Запускается ДО открытия браузера (требование документации)."""

    def __init__(self, port: int = 1455):
        self.port = port
        self._server: asyncio.base_events.Server | None = None
        self._future: asyncio.Future[str] | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://{REDIRECT_HOST}:{self.port}{REDIRECT_PATH}"

    async def __aenter__(self) -> "LoopbackCallbackServer":
        self._future = asyncio.get_running_loop().create_future()
        try:
            self._server = await asyncio.start_server(self._handle, REDIRECT_HOST, self.port)
        except OSError:
            self._server = await asyncio.start_server(self._handle, REDIRECT_HOST, 0)  # порт может отличаться
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def wait(self, timeout: float = 300) -> str:
        assert self._future is not None
        return await asyncio.wait_for(self._future, timeout)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.split(" ")
            target = urlsplit(parts[1]) if len(parts) >= 2 else None
            if target is None or target.path != REDIRECT_PATH:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            body = ("<html><meta charset='utf-8'><body style='font-family:sans-serif'>"
                    "<h3>Glass Agent: ответ получен</h3><p>Вкладку можно закрыть и вернуться в терминал.</p>"
                    "</body></html>").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            if self._future and not self._future.done():
                self._future.set_result(target.query)
        finally:
            await writer.drain()
            writer.close()
