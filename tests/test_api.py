"""REST API: initData, права доступа, чат, файлы, поиск, посты, задачи, входящие, секреты.

Модель — ТЕСТОВАЯ (MockConnector / DevMockConnector, is_mock=True); эмбеддер — тестовый."""
import json
import time

import httpx
import pytest

from app.ai.connectors.base import AIConnectionUnavailable, MockConnector
from app.ai.connectors.dev_mock import DevMockConnector
from app.api.auth import InitDataError, sign_init_data, validate_init_data
from app.api.main import create_app
from app.core.config import get_settings
from app.rag.index import HybridSearcher
from app.services.publisher import LogNotifier

from .conftest import HashEmbedder

BOT = "123456:TEST-TOKEN"
ADMIN, OWNER, EDITOR, STRANGER = 1, 10, 20, 30


def tma(uid, name="U", age=0):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "q",
              "user": json.dumps({"id": uid, "first_name": name})}
    return {"Authorization": "tma " + sign_init_data(fields, BOT)}


@pytest.fixture
def settings_tmp(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path)
    monkeypatch.setattr(s, "api_dev_user_id", None)
    monkeypatch.setattr(s, "admin_telegram_ids", str(ADMIN))
    return s


def make_client(pg, connector):
    _, sm = pg
    emb = HashEmbedder()
    app = create_app(sessionmaker=sm, connector=connector, embedder=emb, searcher=HybridSearcher(emb),
                     notifier=LogNotifier(), bot_token=BOT, init_database=False)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    c.app, c.mock = app, connector
    return c


@pytest.fixture
async def client(pg, settings_tmp):
    async with make_client(pg, MockConnector()) as c:
        yield c


async def new_company(c, slug="aa", **extra):
    r = await c.post("/api/companies", json={"name": "A", "slug": slug, "owner_telegram_id": OWNER, **extra},
                     headers=tma(ADMIN))
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    await c.post(f"/api/companies/{cid}/members", json={"telegram_id": EDITOR, "role": "editor"}, headers=tma(OWNER))
    return cid


def test_init_data_validation():
    good = sign_init_data({"auth_date": str(int(time.time())), "user": json.dumps({"id": 7})}, BOT)
    assert validate_init_data(good, BOT)["user"]["id"] == 7
    with pytest.raises(InitDataError):
        validate_init_data(good.replace("id%22%3A+7", "id%22%3A+8"), BOT)
    with pytest.raises(InitDataError):
        validate_init_data(good, "other:token")
    old = sign_init_data({"auth_date": str(int(time.time()) - 90000), "user": json.dumps({"id": 7})}, BOT)
    with pytest.raises(InitDataError):
        validate_init_data(old, BOT, max_age_s=86400)


async def test_auth_roles_and_isolation(client):
    r = await client.get("/api/companies")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert (await client.get("/api/companies", headers={"Authorization": "tma user=1&hash=bad"})).status_code == 401
    assert (await client.get("/api/companies", headers=tma(OWNER, age=100000))).status_code == 401
    # компании подключает только администратор
    assert (await client.post("/api/companies", json={"name": "X"}, headers=tma(OWNER))).status_code == 403
    cid = await new_company(client)
    assert [c["role"] for c in (await client.get("/api/companies", headers=tma(OWNER))).json()] == ["owner"]
    assert (await client.get("/api/me", headers=tma(ADMIN))).json()["is_admin"] is True
    # чужой пользователь не видит компанию
    assert (await client.get("/api/companies", headers=tma(STRANGER))).json() == []
    for path in (f"/api/companies/{cid}", f"/api/companies/{cid}/chat/history", f"/api/companies/{cid}/analytics",
                 f"/api/companies/{cid}/tasks", f"/api/companies/{cid}/inbox"):
        assert (await client.get(path, headers=tma(STRANGER))).status_code == 404
    assert (await client.post(f"/api/companies/{cid}/chat", json={"message": "x"},
                              headers=tma(STRANGER))).status_code == 404
    # редактор не управляет настройками, участниками и секретами
    assert (await client.patch(f"/api/companies/{cid}", json={"name": "B"}, headers=tma(EDITOR))).status_code == 403
    assert (await client.get(f"/api/companies/{cid}/secrets", headers=tma(EDITOR))).status_code == 403
    assert (await client.post("/api/ai/disconnect", headers=tma(OWNER))).status_code == 403
    # отзыв доступа
    assert (await client.delete(f"/api/companies/{cid}/members/{EDITOR}", headers=tma(OWNER))).json()["removed"]
    assert (await client.get(f"/api/companies/{cid}", headers=tma(EDITOR))).status_code == 404
    audit = (await client.get(f"/api/companies/{cid}/audit", headers=tma(OWNER))).json()
    assert {"member.remove", "company.create"} <= {a["action"] for a in audit}
    meta = (await client.get("/api/meta", headers=tma(OWNER))).json()
    assert "revision" in meta["post_statuses"] and meta["platforms"] == ["telegram", "vk", "max"]
    v = await client.post(f"/api/companies/{cid}/posts", json={"text": ""}, headers=tma(OWNER))
    assert v.status_code == 422 and v.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_chat_history_memory(client):
    cid = await new_company(client)
    client.mock.replies = [json.dumps({"answer": "Здравствуйте!", "remember": [
        {"category": "preference", "content": "Обращаться на «вы»"}]})]
    r = await client.post(f"/api/companies/{cid}/chat", json={"message": "Привет, обращайся на вы"}, headers=tma(EDITOR))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "Здравствуйте!" and body["is_mock"] is True and body["provider"] == "mock"
    hist = (await client.get(f"/api/companies/{cid}/chat/history", headers=tma(OWNER))).json()
    assert [m["role"] for m in hist] == ["user", "assistant"] and hist[1]["is_mock"]
    memory = (await client.get(f"/api/companies/{cid}/chat/memory", headers=tma(OWNER))).json()
    assert memory["long_term"][0]["content"] == "Обращаться на «вы»"
    usage = (await client.get(f"/api/companies/{cid}/usage", headers=tma(OWNER))).json()
    assert usage[0]["calls"] == 1 and usage[0]["provider"] == "mock"


async def test_chat_ai_unavailable_and_quota(client):
    cid = await new_company(client, settings={"ai_daily_call_limit": 1})
    client.mock.replies = [json.dumps({"answer": "раз"})]
    assert (await client.post(f"/api/companies/{cid}/chat", json={"message": "1"}, headers=tma(OWNER))).status_code == 200
    r = await client.post(f"/api/companies/{cid}/chat", json={"message": "2"}, headers=tma(OWNER))
    assert r.status_code == 429 and r.json()["error"]["code"] == "AI_COMPANY_QUOTA_EXCEEDED"

    async def down(*a, **k):
        raise AIConnectionUnavailable("Аккаунт ChatGPT Plus не подключён", reason="not_connected")
    client.mock.complete = down
    await client.patch(f"/api/companies/{cid}", json={"settings": {}}, headers=tma(OWNER))
    r = await client.post(f"/api/companies/{cid}/chat", json={"message": "x"}, headers=tma(OWNER))
    assert r.status_code == 503 and r.json()["error"]["code"] == "AI_CONNECTION_UNAVAILABLE"


async def test_file_upload_search_and_post_flow(client):
    cid = await new_company(client)
    r = await client.post(f"/api/companies/{cid}/files", headers=tma(EDITOR),
                          files={"file": ("../../prices.txt", "Триплекс 8 мм для остекления балконов".encode(),
                                          "text/plain")})
    assert r.status_code == 202 and r.json()["file"] == "prices.txt"
    srcs = (await client.get(f"/api/companies/{cid}/sources", headers=tma(EDITOR))).json()
    assert srcs[0]["type"] == "file" and srcs[0]["status"] == "ok", srcs
    hits = (await client.post(f"/api/companies/{cid}/knowledge/search", json={"query": "триплекс балкон"},
                              headers=tma(EDITOR))).json()
    assert hits and "Триплекс" in hits[0]["text"]
    assert (await client.post(f"/api/companies/{cid}/sources", json={"type": "file", "ref": "/etc"},
                              headers=tma(EDITOR))).status_code == 422

    client.mock.replies = ["Пост про триплекс для балконов"]
    r = await client.post(f"/api/companies/{cid}/posts/generate", headers=tma(EDITOR),
                          json={"topic": "Триплекс", "image": True, "platform": "vk", "submit": True,
                                "proposed_at": "2030-01-01T10:00:00+03:00"})
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["status"] == "pending_approval" and post["platform"] == "vk" and post["checks"]["ok"]
    assert post["generated_by"]["is_mock"] and post["image_meta"]["ai_generated"] is False
    assert "ВКонтакте" in client.app.state.notifier.approvals[0] and "01.01.2030" in client.app.state.notifier.approvals[0]
    pid = post["id"]
    assert (await client.get(f"/api/companies/{cid}/posts/{pid}/image", headers=tma(EDITOR))).status_code == 200

    # редактор не согласует; согласующий возвращает на доработку с комментарием
    assert (await client.post(f"/api/companies/{cid}/posts/{pid}/approve", json={}, headers=tma(EDITOR))).status_code == 403
    r = await client.post(f"/api/companies/{cid}/posts/{pid}/request-changes", json={"comment": "Добавь цену"},
                          headers=tma(OWNER))
    assert r.json()["status"] == "revision" and r.json()["review_comment"] == "Добавь цену"
    r = await client.patch(f"/api/companies/{cid}/posts/{pid}", json={"text": "Триплекс 8 мм, цена по запросу"},
                           headers=tma(EDITOR))
    assert r.json()["status"] == "draft"
    await client.post(f"/api/companies/{cid}/posts/{pid}/submit", headers=tma(EDITOR))
    r = await client.post(f"/api/companies/{cid}/posts/{pid}/approve", json={}, headers=tma(OWNER))
    assert r.json()["status"] == "scheduled" and r.json()["scheduled_at"].startswith("2030-01-01T07:00")
    # смена площадки после согласования снимает согласование
    r = await client.patch(f"/api/companies/{cid}/posts/{pid}", json={"platform": "telegram"}, headers=tma(EDITOR))
    assert r.json()["status"] == "draft" and r.json()["content_approved"] is False
    assert (await client.post(f"/api/companies/{cid}/posts/{pid}/schedule", json={},
                              headers=tma(OWNER))).status_code == 409
    a = (await client.get(f"/api/companies/{cid}/posts/{pid}/analytics", headers=tma(OWNER))).json()
    assert a["metrics"] is None
    stats = (await client.get(f"/api/companies/{cid}/analytics", headers=tma(OWNER))).json()
    assert stats["posts_by_status"] == {"draft": 1} and stats["knowledge_documents"] == {"file": 1}


async def test_checks_block_submit(client):
    cid = await new_company(client, settings={"brand": {"banned_words": ["дешёвый"]}})
    pid = (await client.post(f"/api/companies/{cid}/posts", json={"text": "Самый дешёвый триплекс [вставьте цену]"},
                             headers=tma(EDITOR))).json()["id"]
    r = await client.post(f"/api/companies/{cid}/posts/{pid}/submit", headers=tma(EDITOR))
    assert r.status_code == 409 and "дешёвый" in r.json()["error"]["message"]
    post = (await client.get(f"/api/companies/{cid}/posts/{pid}", headers=tma(EDITOR))).json()
    assert post["status"] == "draft" and len(post["checks"]["errors"]) == 2


async def test_tasks_api(client):
    cid = await new_company(client)
    tasks = (await client.get(f"/api/companies/{cid}/tasks", headers=tma(OWNER))).json()
    assert {t["kind"] for t in tasks} == {"report", "sync_sources", "collect_results", "collect_comments", "agent"}
    report = next(t for t in tasks if t["kind"] == "report")
    r = await client.post(f"/api/companies/{cid}/tasks/{report['id']}/run", headers=tma(EDITOR))
    assert r.status_code == 200 and r.json()["last_status"] == "ok" and r.json()["run_count"] == 1
    sent = client.app.state.notifier.sent[-1][1]
    assert "Отчёт" in sent
    bad = await client.post(f"/api/companies/{cid}/tasks", json={"kind": "agent", "schedule": {"every_minutes": 60}},
                            headers=tma(OWNER))
    assert bad.status_code == 422
    t = (await client.post(f"/api/companies/{cid}/tasks", headers=tma(OWNER), json={
        "kind": "agent", "title": "Идеи", "params": {"prompt": "Предложи темы"},
        "schedule": {"daily_at": "09:30", "weekdays": [1], "tz": "Europe/Moscow"}})).json()
    assert t["enabled"] and t["next_run_at"]
    r = await client.patch(f"/api/companies/{cid}/tasks/{t['id']}", json={"enabled": False}, headers=tma(OWNER))
    assert r.json()["enabled"] is False


async def test_secrets_never_returned(client):
    cid = await new_company(client)
    assert (await client.put(f"/api/companies/{cid}/secrets/vk_token", json={"value": "vk1.a.SECRET1234"},
                             headers=tma(OWNER))).json()["saved"]
    assert (await client.put(f"/api/companies/{cid}/secrets/bad", json={"value": "x"},
                             headers=tma(OWNER))).status_code == 422
    listed = (await client.get(f"/api/companies/{cid}/secrets", headers=tma(OWNER))).text
    assert "SECRET" not in listed and "…1234" in listed
    assert (await client.delete(f"/api/companies/{cid}/secrets/vk_token", headers=tma(OWNER))).json()["deleted"]


async def test_dev_mock_full_agent_flow(pg, settings_tmp):
    """Сценарий ТЗ на ТЕСТОВОЙ модели: поручение → черновик с картинкой → согласование."""
    async with make_client(pg, DevMockConnector()) as c:
        cid = await new_company(c)
        r = await c.post(f"/api/companies/{cid}/chat", json={"message": "Подготовь пост про энергосберегающее стекло"},
                         headers=tma(EDITOR))
        body = r.json()
        assert body["is_mock"] and body["answer"].startswith("[ТЕСТОВАЯ МОДЕЛЬ]")
        assert [s["tool"] for s in body["steps"]] == ["create_post_draft", "prepare_post_image", "submit_for_approval"]
        post = (await c.get(f"/api/companies/{cid}/posts/{body['created_post_ids'][0]}", headers=tma(EDITOR))).json()
        assert post["status"] == "pending_approval" and post["has_image"] and post["image_url"].endswith("/image")
        assert c.app.state.notifier.approvals  # согласующим ушло сообщение
        status = (await c.get("/api/ai/connection", headers=tma(EDITOR))).json()
        assert status["is_mock"] is True and "ТЕСТОВАЯ" in status["warning"]
