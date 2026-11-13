"""Базовый интерфейс AI-коннектора и ошибки.

Коннектор можно заменить (критерий ТЗ «замена AI-модели без потери данных»):
история, память и знания хранятся в PostgreSQL, а не у провайдера модели.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

AI_CONNECTION_UNAVAILABLE = "AI_CONNECTION_UNAVAILABLE"


class AIError(RuntimeError):
    code: str = "AI_ERROR"

    def __init__(self, message: str, *, reason: str | None = None, http_status: int | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason, "message": str(self)}


class AIConnectionUnavailable(AIError):
    """Нет подключения/разрешения на использование подписки. Не подменяется платным API."""
    code = AI_CONNECTION_UNAVAILABLE


class AIUsageLimitExceeded(AIError):
    """Исчерпан лимит подписки ChatGPT Plus (общее 5-часовое окно)."""
    code = "AI_USAGE_LIMIT_EXCEEDED"


class AITemporarilyUnavailable(AIError):
    code = "AI_TEMPORARILY_UNAVAILABLE"


class AIRequestError(AIError):
    code = "AI_REQUEST_ERROR"


Role = Literal["user", "assistant", "developer"]


@dataclass
class AIMessage:
    role: Role
    content: str


@dataclass
class AIResult:
    text: str
    model: str
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"
    provider: str = ""
    is_mock: bool = False


class AIConnector(Protocol):
    name: str

    async def status(self) -> dict[str, Any]: ...
    async def stream(self, messages: list[AIMessage], *, instructions: str | None = None,
                     model: str | None = None) -> AsyncIterator[str]: ...
    async def complete(self, messages: list[AIMessage], *, instructions: str | None = None,
                       model: str | None = None) -> AIResult: ...


class MockConnector:
    """ТЕСТОВАЯ модель для автотестов. Никогда не используется как ответ ChatGPT:
    результат помечен is_mock=True и provider="mock"."""

    name = "mock"

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []

    async def status(self) -> dict[str, Any]:
        return {"provider": "mock", "connected": True, "is_mock": True}

    async def complete(self, messages, *, instructions=None, model=None) -> AIResult:
        self.calls.append({"messages": messages, "instructions": instructions})
        text = self.replies.pop(0) if self.replies else f"[MOCK] {messages[-1].content[:200]}"
        return AIResult(text=text, model="mock", provider="mock", is_mock=True)

    async def stream(self, messages, *, instructions=None, model=None):
        res = await self.complete(messages, instructions=instructions, model=model)
        yield res.text
