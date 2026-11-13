"""ChatGPTPlusConnector: SSE, ошибки подписки, лимиты. /v1/responses ЗАМОКАН — это НЕ ответ настоящего ChatGPT."""
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from app.ai.auth.token_refresh import TokenManager
from app.ai.auth.token_storage import EncryptedTokenStorage
from app.ai.connectors.base import AIConnectionUnavailable, AIMessage, AIUsageLimitExceeded, MockConnector
from app.ai.connectors.chatgpt_plus import ChatGPTPlusConnector
from tests.test_chatgpt_oauth import creds


def sse(*events):
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


def connector(tmp_path, handler, refresh_handler=None):
    st = EncryptedTokenStorage(tmp_path / "c.enc", Fernet(Fernet.generate_key()))
    st.save(creds())
    tm = TokenManager(st, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(refresh_handler or (lambda r: httpx.Response(500)))))
    return ChatGPTPlusConnector(tm, model="gpt-test", transport=httpx.MockTransport(handler), limit_cooldown_s=60)


OK_STREAM = sse(("response.created", {"response": {"id": "r1"}}),
                ("response.output_text.delta", {"delta": "Привет, "}),
                ("response.output_text.delta", {"delta": "мир"}),
                ("response.completed", {"response": {"id": "r1", "model": "gpt-test", "status": "completed",
                                                     "usage": {"input_tokens": 5, "output_tokens": 2}}}))


async def test_complete_streams_and_sends_allowed_body(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-used")
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, text=OK_STREAM, headers={"content-type": "text/event-stream"})

    res = await connector(tmp_path, handler).complete(
        [AIMessage("user", "hi"), AIMessage("assistant", "hello"), AIMessage("user", "ещё")], instructions="Ты маркетолог")
    assert res.text == "Привет, мир" and res.usage["output_tokens"] == 2 and not res.is_mock
    assert seen["auth"] == "Bearer AT1"  # OAuth-токен ChatGPT, не OPENAI_API_KEY
    b = seen["body"]
    assert b["store"] is False and b["stream"] is True and b["instructions"] == "Ты маркетолог"
    assert [i["role"] for i in b["input"]] == ["user", "assistant", "user"]
    assert not {"temperature", "max_output_tokens", "previous_response_id", "top_p"} & b.keys()


async def test_usage_limit_pauses(tmp_path):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded",
                                                   "message": "limit"}})

    c = connector(tmp_path, handler)
    with pytest.raises(AIUsageLimitExceeded):
        await c.complete([AIMessage("user", "x")])
    with pytest.raises(AIUsageLimitExceeded) as e:
        await c.complete([AIMessage("user", "x")])
    assert e.value.reason == "paused" and n["c"] == 1  # второй запрос не отправлялся


async def test_not_eligible_is_connection_unavailable(tmp_path):
    c = connector(tmp_path, lambda r: httpx.Response(403, json={"error": {"code": "subscription_sharing_user_not_eligible"}}))
    with pytest.raises(AIConnectionUnavailable) as e:
        await c.complete([AIMessage("user", "x")])
    assert e.value.code == "AI_CONNECTION_UNAVAILABLE"


async def test_failed_mid_stream(tmp_path):
    body = sse(("response.output_text.delta", {"delta": "на"}),
               ("response.failed", {"response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}))
    c = connector(tmp_path, lambda r: httpx.Response(200, text=body))
    with pytest.raises(AIUsageLimitExceeded):
        await c.complete([AIMessage("user", "x")])


async def test_401_refreshes_once(tmp_path):
    tokens = []

    def handler(req):
        tokens.append(req.headers["authorization"])
        if len(tokens) == 1:
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, text=OK_STREAM)

    refresh = lambda r: httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})
    res = await connector(tmp_path, handler, refresh).complete([AIMessage("user", "x")])
    assert tokens == ["Bearer AT1", "Bearer AT2"] and res.text == "Привет, мир"


async def test_list_models_filters_visibility(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"data": [{"slug": "m1", "display_name": "M1", "visibility": "list"},
                                                  {"slug": "hidden", "visibility": "hide"}]})
    assert await connector(tmp_path, handler).list_models() == [{"slug": "m1", "display_name": "M1"}]


async def test_mock_connector_is_marked():
    r = await MockConnector(["ok"]).complete([AIMessage("user", "x")])
    assert r.is_mock and r.provider == "mock"
