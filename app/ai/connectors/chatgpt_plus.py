"""ChatGPTPlusConnector — запросы к модели за счёт подписки ChatGPT Plus.

Официальный путь «ChatGPT plan usage» (Sign in with ChatGPT, preview):
POST https://api.openai.com/v1/responses с OAuth access token пользователя,
обязательно store=false и stream=true, без temperature/max_output_tokens/
previous_response_id (история передаётся целиком в input).

Это НЕ платный OpenAI API: OPENAI_API_KEY не используется и не читается.
При отсутствии разрешения возвращается AI_CONNECTION_UNAVAILABLE —
никакого молчаливого переключения на другой провайдер.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..auth.oauth import REQUIRED_SCOPE, revoke_refresh_token
from ..auth.token_refresh import TokenManager
from .base import (
    AIConnectionUnavailable,
    AIMessage,
    AIRequestError,
    AIResult,
    AITemporarilyUnavailable,
    AIUsageLimitExceeded,
)

log = logging.getLogger(__name__)
API_BASE = "https://api.openai.com/v1"
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"

# error.code → (класс ошибки, повторять ли)
ERROR_MAP: dict[str, tuple[type, bool]] = {
    "subscription_sharing_usage_limit_exceeded": (AIUsageLimitExceeded, False),
    "subscription_sharing_usage_unavailable": (AITemporarilyUnavailable, True),
    "subscription_sharing_user_unavailable": (AITemporarilyUnavailable, True),
    "subscription_sharing_user_not_eligible": (AIConnectionUnavailable, False),
    "subscription_sharing_route_not_supported": (AIConnectionUnavailable, False),
    "subscription_sharing_invalid_user": (AIConnectionUnavailable, False),
    "chatpass_v2_scope_not_authorized": (AIConnectionUnavailable, False),
    "chatpass_v2_invalid_authorization_context": (AIConnectionUnavailable, False),
    "subscription_sharing_unsupported_capability": (AIRequestError, False),
}


def _error_from(status: int, body: Any) -> Exception:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or ""
        msg = err.get("message") or code
        param = err.get("param")
    else:
        code, param = "", None
        msg = (body.get("detail") if isinstance(body, dict) else None) or f"HTTP {status}"
    cls, _ = ERROR_MAP.get(code, (None, False))
    if cls is None:
        if status == 429:
            cls = AIUsageLimitExceeded
        elif status in (401, 403):
            cls = AIConnectionUnavailable
        elif status >= 500:
            cls = AITemporarilyUnavailable
        else:
            cls = AIRequestError
    text = str(msg)
    if cls is AIUsageLimitExceeded:
        text = f"Исчерпан лимит ChatGPT Plus. Проверьте: {USAGE_SETTINGS_URL}"
    return cls(text, reason=code or None, http_status=status, details={"param": param} if param else {})


def _retryable(err: Exception) -> bool:
    if not isinstance(err, AITemporarilyUnavailable):
        return False
    _, retry = ERROR_MAP.get(getattr(err, "reason", None) or "", (None, True))
    return retry


def build_input(messages: list[AIMessage]) -> list[dict[str, Any]]:
    """История диалога в формате input Responses API (system-сообщения запрещены — только instructions/developer)."""
    items = []
    for m in messages:
        if not m.content:
            continue
        items.append({"role": m.role, "content": m.content})
    return items


class ChatGPTPlusConnector:
    name = "chatgpt_plus"

    def __init__(self, tokens: TokenManager, *, model: str | None = None, limit_cooldown_s: int = 900,
                 transport: httpx.AsyncBaseTransport | None = None, api_base: str = API_BASE,
                 max_retries: int = 2, retry_base_s: float = 1.0):
        self.tokens = tokens
        self.model = model
        self.limit_cooldown_s = limit_cooldown_s
        self._transport = transport
        self.api_base = api_base
        self._paused_until = 0.0
        self.max_retries = max_retries
        self.retry_base_s = retry_base_s
        self._models_cache: list[dict[str, str]] | None = None

    def _client(self, timeout: float = 120) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15), transport=self._transport)

    # ------------------------------------------------------------------ status / models

    async def status(self) -> dict[str, Any]:
        creds = self.tokens.load()
        if creds is None:
            return {"provider": self.name, "connected": False, "error": "AI_CONNECTION_UNAVAILABLE",
                    "reason": "not_connected"}
        info = {"provider": self.name, "connected": True, **creds.public_view(),
                "plan_usage_permission": REQUIRED_SCOPE in creds.scopes,
                "model": self.model,
                "paused_until": self._paused_until if self._paused_until > time.time() else None,
                "uses_paid_api_key": False}
        if not info["plan_usage_permission"]:
            info.update(error="AI_CONNECTION_UNAVAILABLE", reason="scope_missing")
        return info

    async def list_models(self) -> list[dict[str, str]]:
        creds = await self.tokens.get_credentials()
        async with self._client(30) as client:
            r = await client.get(f"{self.api_base}/models", headers={"Authorization": f"Bearer {creds.access_token}"})
        if r.status_code != 200:
            raise _error_from(r.status_code, _json(r))
        body = r.json()
        raw = (body.get("data") or body.get("models") or []) if isinstance(body, dict) else body
        models = []
        for m in raw:
            if m.get("visibility", "list") != "list":
                continue
            slug = m.get("slug") or m.get("id")
            if slug:
                models.append({"slug": slug, "display_name": m.get("display_name") or slug})
        self._models_cache = models
        return models

    async def _resolve_model(self, model: str | None) -> str:
        if model or self.model:
            return model or self.model  # type: ignore[return-value]
        models = self._models_cache or await self.list_models()
        if not models:
            raise AIConnectionUnavailable("Для аккаунта нет доступных моделей", reason="no_models")
        return models[0]["slug"]

    # ------------------------------------------------------------------ inference

    async def _events(self, messages: list[AIMessage], instructions: str | None,
                      model: str | None) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if self._paused_until > time.time():
            raise AIUsageLimitExceeded(
                f"Запросы приостановлены после исчерпания лимита ChatGPT Plus. Проверьте: {USAGE_SETTINGS_URL}",
                reason="paused")
        creds = await self.tokens.get_credentials()
        body: dict[str, Any] = {
            "model": await self._resolve_model(model),
            "input": build_input(messages),
            "store": False,
            "stream": True,
        }
        if instructions:
            body["instructions"] = instructions
        refreshed = False
        for attempt in range(self.max_retries + 1):
            async with self._client() as client:
                async with client.stream("POST", f"{self.api_base}/responses", json=body, headers={
                    "Authorization": f"Bearer {creds.access_token}",
                    "Content-Type": "application/json", "Accept": "text/event-stream",
                }) as r:
                    if r.status_code == 401 and not refreshed:
                        refreshed = True  # одно принудительное обновление токена и повтор
                        creds = await self.tokens.get_credentials(force_refresh=True)
                        continue
                    if r.status_code != 200:
                        await r.aread()
                        err = _error_from(r.status_code, _json(r))
                        self._maybe_pause(err)
                        # 503 *_unavailable: ограниченный повтор с паузой (errors-and-recovery), учётные данные не трогаем
                        if _retryable(err) and attempt < self.max_retries:
                            delay = self.retry_base_s * 3 ** attempt
                            log.warning("ChatGPT временно недоступен (%s), повтор через %.0f с", err.reason, delay)
                            await asyncio.sleep(delay)
                            continue
                        raise err
                    async for event, data in _iter_sse(r):
                        yield event, data
                    return
        raise AITemporarilyUnavailable("ChatGPT недоступен после повторов", reason="retries_exhausted")

    def _maybe_pause(self, err: Exception) -> None:
        if isinstance(err, AIUsageLimitExceeded):
            self._paused_until = time.time() + self.limit_cooldown_s

    async def stream(self, messages: list[AIMessage], *, instructions: str | None = None,
                     model: str | None = None) -> AsyncIterator[str]:
        async for event, data in self._events(messages, instructions, model):
            if event == "response.output_text.delta":
                yield data.get("delta", "")
            elif event in ("response.failed", "error", "response.incomplete"):
                raise self._stream_error(event, data)

    async def complete(self, messages: list[AIMessage], *, instructions: str | None = None,
                       model: str | None = None) -> AIResult:
        parts: list[str] = []
        final: dict[str, Any] | None = None
        async for event, data in self._events(messages, instructions, model):
            if event == "response.output_text.delta":
                parts.append(data.get("delta", ""))
            elif event == "response.completed":
                final = data.get("response", data)
            elif event in ("response.failed", "error", "response.incomplete"):
                raise self._stream_error(event, data)
        if final is None:
            raise AITemporarilyUnavailable("Поток завершился без response.completed", reason="stream_cut")
        text = "".join(parts) or _output_text(final)
        return AIResult(text=text, model=final.get("model", ""), response_id=final.get("id"),
                        usage=final.get("usage") or {}, status=final.get("status", "completed"), provider=self.name)

    def _stream_error(self, event: str, data: dict[str, Any]) -> Exception:
        resp = data.get("response", {}) if isinstance(data.get("response"), dict) else {}
        if event == "response.incomplete":
            reason = (resp.get("incomplete_details") or {}).get("reason")
            return AIRequestError(f"Ответ модели неполный: {reason}", reason="incomplete")
        err = resp.get("error") or data.get("error") or data
        e = _error_from(429 if "limit" in str(err.get("code", "")) else 400, {"error": err})
        self._maybe_pause(e)
        return e

    # ------------------------------------------------------------------ disconnect

    async def disconnect(self) -> None:
        creds = self.tokens.load()
        if creds is not None:
            try:
                async with self._client(30) as client:
                    await revoke_refresh_token(client, client_id=creds.client_id, refresh_token=creds.refresh_token)
            except Exception as e:  # noqa: BLE001 — локально удаляем в любом случае
                log.warning("Отзыв токена не удался: %s", e)
        self.tokens.storage.clear()
        self._models_cache = None


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"detail": r.text[:500]}


def _output_text(response: dict[str, Any]) -> str:
    out = []
    for item in response.get("output") or []:
        for c in item.get("content") or []:
            if c.get("type") in ("output_text", "text"):
                out.append(c.get("text", ""))
    return "".join(out)


async def _iter_sse(r: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    event, data_lines = "", []
    async for line in r.aiter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                if raw.strip() != "[DONE]":
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {"raw": raw}
                    yield event or payload.get("type", "message"), payload
            event, data_lines = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        payload = json.loads("\n".join(data_lines))
        yield event or payload.get("type", "message"), payload
