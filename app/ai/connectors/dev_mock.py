"""ТЕСТОВАЯ модель для разработки фронтенда и демо-прогона без ChatGPT (AI_PROVIDER=mock_dev).

Никогда не включается сама: только явной настройкой. Каждый ответ помечен is_mock=True,
provider="mock_dev" и начинается с «[ТЕСТОВАЯ МОДЕЛЬ]» — его нельзя выдать за ответ ChatGPT.
Детерминированно проходит сценарий агента: поиск → черновик поста → картинка → согласование → ответ.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import AIMessage, AIResult

LABEL = "[ТЕСТОВАЯ МОДЕЛЬ]"
_TOOL_RESULT = re.compile(r"^Результат инструмента (\w+):\n(.*)$", re.S)


class DevMockConnector:
    name = "mock_dev"

    async def status(self) -> dict[str, Any]:
        return {"provider": self.name, "connected": True, "is_mock": True, "plan_usage_permission": False,
                "uses_paid_api_key": False,
                "warning": "Включена ТЕСТОВАЯ модель (AI_PROVIDER=mock_dev). Ответы не от ChatGPT."}

    def _result(self, text: str) -> AIResult:
        return AIResult(text=text, model="mock-dev", provider=self.name, is_mock=True,
                        usage={"input_tokens": 0, "output_tokens": 0})

    async def complete(self, messages: list[AIMessage], *, instructions: str | None = None,
                       model: str | None = None) -> AIResult:
        last = messages[-1].content if messages else ""
        if instructions and "Протокол ответа" in instructions:
            return self._result(json.dumps(self._agent_step(messages), ensure_ascii=False))
        if instructions and "текст поста" in instructions:
            topic = re.search(r"на тему:?\s*(.+)$", last.strip(), re.M)
            return self._result(f"{LABEL} Пост на тему «{(topic.group(1) if topic else 'публикация')[:120]}». "
                                "Здесь будет текст от ChatGPT после подключения аккаунта. #тест")
        if instructions and "ответ" in instructions.lower() and "комментарий" in instructions.lower():
            return self._result(f"{LABEL} Спасибо за комментарий! Менеджер скоро ответит подробнее.")
        return self._result(f"{LABEL} Ответ модели появится после подключения ChatGPT. Запрос: {last[-300:]}")

    def _agent_step(self, messages: list[AIMessage]) -> dict[str, Any]:
        last = messages[-1]
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        m = _TOOL_RESULT.match(last.content) if last.role == "developer" else None
        if m is None:
            low = user_text.lower()
            if "пост" in low or "публикац" in low:
                return {"tool": "create_post_draft", "args": {"topic": user_text[:200]}}
            if "отч" in low or "статист" in low or "аналит" in low:
                return {"tool": "build_report", "args": {"days": 7}}
            if "контент" in low and "план" in low:
                return {"tool": "generate_content_plan", "args": {"period": "неделя"}}
            return {"tool": "search_company_knowledge", "args": {"query": user_text[:200]}}
        tool, payload = m.group(1), m.group(2)
        try:
            data = json.loads(payload.removesuffix("…(обрезано)"))
        except json.JSONDecodeError:
            data = {}
        pid = data.get("post_id") if isinstance(data, dict) else None
        if tool == "create_post_draft" and pid:
            return {"tool": "prepare_post_image", "args": {"post_id": pid}}
        if tool == "prepare_post_image" and pid:
            return {"tool": "submit_for_approval", "args": {"post_id": pid}}
        if tool == "submit_for_approval" and pid:
            return {"answer": f"{LABEL} Черновик поста {pid} с изображением отправлен на согласование.",
                    "current_task": "Ждём согласования поста"}
        if tool == "search_company_knowledge" and isinstance(data, list):
            found = "; ".join(f"[{h['n']}] {h['title']} — {h['url']}" for h in data[:3]) or "ничего не найдено"
            return {"answer": f"{LABEL} Нашёл в базе знаний: {found}"}
        return {"answer": f"{LABEL} Инструмент {tool} выполнен. Результат: {payload[:500]}"}

    async def stream(self, messages, *, instructions=None, model=None):
        yield (await self.complete(messages, instructions=instructions, model=model)).text
