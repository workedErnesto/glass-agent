"""Агент: сам выбирает инструменты по JSON-протоколу, с ограничением шагов, времени и вызовов модели.

JSON-протокол в тексте ответа работает с любой моделью и не зависит от
поддержки нативных function tools в подключении ChatGPT Plus.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .connectors.base import AIConnector, AIMessage, AIResult
from .tools import BudgetExceeded, ToolContext, ToolError, call_tool

log = logging.getLogger(__name__)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_action(text: str) -> dict[str, Any] | None:
    """Первый JSON-объект из ответа модели (допускает ```json ...``` и текст вокруг)."""
    s = _FENCE.sub("", text.strip())
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(s[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("tool" in obj or "answer" in obj):
                return obj
    return None


@dataclass
class AgentStep:
    tool: str
    args: dict[str, Any]
    ok: bool
    result_preview: str


@dataclass
class AgentResult:
    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    remember: list[dict[str, Any]] = field(default_factory=list)
    current_task: str | None = None
    llm_calls: int = 0
    provider: str = ""
    model: str = ""
    is_mock: bool = False
    stopped_reason: str | None = None


class Agent:
    def __init__(self, connector: AIConnector, *, max_steps: int = 6, max_result_chars: int = 6000):
        self.connector = connector
        self.max_steps = max_steps
        self.max_result_chars = max_result_chars

    async def _call(self, ctx: ToolContext, messages: list[AIMessage], instructions: str) -> AIResult:
        return await ctx.complete(messages, instructions=instructions)

    async def run(self, ctx: ToolContext, instructions: str, messages: list[AIMessage]) -> AgentResult:
        msgs = list(messages)
        out = AgentResult(answer="")
        mock_seen = False
        last: AIResult | None = None
        try:
            for step in range(self.max_steps + 1):
                if step == self.max_steps:
                    msgs.append(AIMessage("developer", "Лимит шагов исчерпан. Дай финальный ответ JSON с полем answer "
                                                       "по уже полученным данным."))
                last = await self._call(ctx, msgs, instructions)
                mock_seen |= last.is_mock
                action = parse_action(last.text)
                if action is None or "answer" in action or step == self.max_steps:
                    if action is None:
                        out.answer = last.text.strip()
                    else:
                        out.answer = str(action.get("answer") or "").strip() or last.text.strip()
                        out.remember = [r for r in action.get("remember") or [] if isinstance(r, dict)]
                        task = action.get("current_task")
                        out.current_task = str(task).strip() if task else None
                    if step == self.max_steps:
                        out.stopped_reason = "max_steps"
                    break
                name = str(action.get("tool", ""))
                args = action.get("args") if isinstance(action.get("args"), dict) else {}
                msgs.append(AIMessage("assistant", json.dumps({"tool": name, "args": args}, ensure_ascii=False)))
                try:
                    result = await call_tool(ctx, name, args)
                    payload, ok = json.dumps(result, ensure_ascii=False, default=str), True
                except BudgetExceeded:
                    raise
                except ToolError as e:
                    payload, ok = json.dumps({"error": str(e)}, ensure_ascii=False), False
                except Exception as e:  # noqa: BLE001 — ошибка инструмента не должна ронять диалог
                    log.exception("Инструмент %s упал", name)
                    payload, ok = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), False
                if ctx.last_result is not None:
                    mock_seen |= ctx.last_result.is_mock
                if len(payload) > self.max_result_chars:
                    payload = payload[:self.max_result_chars] + "…(обрезано)"
                out.steps.append(AgentStep(name, args, ok, payload[:300]))
                msgs.append(AIMessage("developer", f"Результат инструмента {name}:\n{payload}"))
        except BudgetExceeded as e:
            out.stopped_reason = "budget"
            done = "; ".join(f"{s.tool}{'' if s.ok else ' (ошибка)'}" for s in out.steps) or "нет"
            out.answer = (out.answer or f"Остановлено: {e}. Выполненные шаги: {done}. "
                          "Уточните запрос или повторите позже.")
        out.llm_calls = ctx.budget.llm_calls
        if last is not None:
            out.provider, out.model = last.provider, last.model
        out.is_mock = mock_seen
        return out
