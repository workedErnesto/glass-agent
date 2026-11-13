"""VK и MAX (моки API по документации), регулярные задачи, комментарии VK, повтор 503 ChatGPT.

Реальные VK/MAX не вызываются: проверяется логика, а не доступ к настоящим аккаунтам."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.ai.connectors.base import MockConnector
from app.ai.orchestrator import Orchestrator
from app.db.models import Document, InboxItem, Memory, Post, ScheduledTask
from app.rag.index import HybridSearcher
from app.services import inbox, posts as P, secrets, tasks
from app.services.platforms import MaxAPI, MaxPublisher, PublisherHub
from app.services.publisher import LogNotifier, run_due
from app.services.sources import add_source, create_company

from .conftest import HashEmbedder


def vk_handler(calls):
    def handle(req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(req.content.decode(errors="ignore")).items()} \
            if req.headers.get("content-type", "").startswith("application/x-www-form") else {}
        calls.append((method, form))
        if req.url.host == "upload.vk.test":
            return httpx.Response(200, json={"server": 1, "photo": "[{\"p\":1}]", "hash": "h"})
        resp = {
            "photos.getWallUploadServer": {"upload_url": "https://upload.vk.test/x"},
            "photos.saveWallPhoto": [{"id": 9, "owner_id": -123}],
            "wall.post": {"post_id": 77},
            "wall.getComments": {"count": 2, "items": [
                {"id": 1, "from_id": 5, "date": 1700000000, "text": "Сколько стоит?"},
                {"id": 2, "from_id": -123, "date": 1700000100, "text": "Ответ сообщества"}],
                "profiles": [{"id": 5, "first_name": "Иван", "last_name": "П"}], "groups": []},
            "wall.createComment": {"comment_id": 3},
        }[method]
        return httpx.Response(200, json={"response": resp})
    return handle


async def approved_post(sm, platform, *, image=None, channels=None):
    async with sm() as s, s.begin():
        c = await create_company(s, "Компания", slug="cc", settings={"channels": channels or {}},
                                 default_tasks=False)
        p = await P.create_draft(s, c.id, text="Текст поста", platform=platform)
        if image:
            await P.edit_content(s, p, image_path=str(image))
        await P.approve(s, p, user_id=None, scheduled_at=datetime.now(timezone.utc))
        return c.id, p.id


async def test_vk_publish_with_photo(pg, tmp_path):
    _, sm = pg
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    cid, pid = await approved_post(sm, "vk", image=img, channels={"vk": "club123"})
    async with sm() as s, s.begin():
        await secrets.set_secret(s, cid, "vk_token", "vk1.a.test")
    calls = []
    hub = PublisherHub(transport=httpx.MockTransport(vk_handler(calls)))
    assert await run_due(sm, hub, LogNotifier()) == {str(pid): "published"}
    methods = [m for m, _ in calls]
    assert methods == ["photos.getWallUploadServer", "x", "photos.saveWallPhoto", "wall.post"]
    post_form = calls[-1][1]
    assert post_form["owner_id"] == "-123" and post_form["from_group"] == "1" and post_form["attachments"] == "photo-123_9"
    async with sm() as s:
        p = await s.get(Post, pid)
        assert p.external_url == "https://vk.com/wall-123_77" and p.target_chat_id == "-123"


async def test_vk_without_token_fails_permanently(pg):
    _, sm = pg
    _, pid = await approved_post(sm, "vk", channels={"vk": "123"})
    notifier = LogNotifier()
    assert await run_due(sm, PublisherHub(), notifier) == {str(pid): "failed"}
    assert "vk_token" in notifier.sent[0][1] and notifier.admin  # администратор уведомлён


def max_handler(calls, not_ready=1):
    state = {"not_ready": not_ready}

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, dict(req.url.params), req.headers.get("authorization")))
        if req.url.path == "/uploads":
            return httpx.Response(200, json={"url": "https://upload.max.test/u"})
        if req.url.host == "upload.max.test":
            return httpx.Response(200, json={"photos": {"abc": {"token": "img-token"}}})
        if req.url.path == "/messages":
            if state["not_ready"]:
                state["not_ready"] -= 1
                return httpx.Response(400, json={"code": "attachment.not.ready", "message": "not processed"})
            body = json.loads(req.content)
            assert body["attachments"] == [{"type": "image", "payload": {"token": "img-token"}}]
            return httpx.Response(200, json={"message": {"body": {"mid": "mid.1", "text": body["text"]},
                                                          "stat": {"url": "https://max.ru/c/1/mid.1"}}})
        return httpx.Response(404, json={"code": "not.found", "message": "?"})
    return handle


async def test_max_publish_with_image_and_retry(pg, tmp_path):
    _, sm = pg
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    _, pid = await approved_post(sm, "max", image=img, channels={"max": "-7001"})
    calls = []
    pub = MaxPublisher(MaxAPI("max-token", transport=httpx.MockTransport(max_handler(calls))), retry_base_s=0)
    assert await run_due(sm, PublisherHub(overrides={"max": pub}), LogNotifier()) == {str(pid): "published"}
    assert all(c[3] == "max-token" for c in calls)  # токен — в заголовке Authorization
    assert [c[1] for c in calls].count("/messages") == 2 and calls[-1][2] == {"chat_id": "-7001"}
    async with sm() as s:
        p = await s.get(Post, pid)
        assert p.external_message_id == "mid.1" and p.external_url == "https://max.ru/c/1/mid.1"


# --------------------------------------------------------------------------- задачи

def test_next_run_daily_and_interval():
    after = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)  # пятница, 13:00 МСК
    nxt = tasks.next_run({"daily_at": "09:00", "weekdays": [0], "tz": "Europe/Moscow"}, after)
    assert nxt == datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)  # понедельник 09:00 МСК
    assert tasks.next_run({"every_minutes": 60}, after) == after + timedelta(hours=1)
    with pytest.raises(tasks.TaskConfigError):
        tasks.validate_schedule({"daily_at": "25:00"})
    with pytest.raises(tasks.TaskConfigError):
        tasks.validate_schedule({"every_minutes": 1})


async def test_scheduled_tasks_run_without_user(pg):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "Компания", slug="cc")
        t = await s.scalar(select(ScheduledTask).where(ScheduledTask.company_id == c.id, ScheduledTask.kind == "agent"))
        t.enabled = True
        cid = c.id
    notifier = LogNotifier()
    class ByPurpose(MockConnector):  # ответ зависит от вида запроса, а не от порядка задач
        async def complete(self, messages, *, instructions=None, model=None):
            self.replies = [json.dumps({"answer": "План готов"}) if "Протокол ответа" in (instructions or "")
                            else "Вывод: публикуйте чаще"]
            return await super().complete(messages, instructions=instructions, model=model)
    mock = ByPurpose()
    deps = tasks.TaskDeps(notifier=notifier, embedder=HashEmbedder(),
                          orchestrator=Orchestrator(mock, HybridSearcher(HashEmbedder())))
    future = datetime.now(timezone.utc) + timedelta(days=8)
    done = await tasks.run_due_tasks(sm, deps, now=future)
    assert len(done) == 5 and set(done.values()) == {"ok"}, done
    texts = "\n".join(t for _, t in notifier.sent)
    assert "План готов" in texts and "Отчёт" in texts and "Вывод: публикуйте чаще" in texts
    assert "[ТЕСТОВАЯ МОДЕЛЬ]" in texts  # и в ответе агента, и в выводах отчёта
    async with sm() as s:
        rows = (await s.scalars(select(ScheduledTask).where(ScheduledTask.company_id == cid))).all()
        assert all(r.run_count == 1 and r.next_run_at > future and r.running_since is None for r in rows)
        hist = (await s.scalars(select(Memory).where(Memory.company_id == cid, Memory.category == "analysis"))).all()
        assert hist  # отчёт записан в эпизодическую память
    assert await tasks.run_due_tasks(sm, deps, now=future) == {}  # повторно не запускается


async def test_task_error_notifies_admin(pg):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "К", slug="kk", default_tasks=False)
        t = await tasks.create_task(s, c.id, kind="agent", params={"prompt": "x"}, schedule={"every_minutes": 5})
        tid = t.id

    class Down(MockConnector):
        async def complete(self, *a, **k):
            from app.ai.connectors.base import AIConnectionUnavailable
            raise AIConnectionUnavailable("нет", reason="not_connected")
    notifier = LogNotifier()
    deps = tasks.TaskDeps(notifier=notifier, orchestrator=Orchestrator(Down(), HybridSearcher(HashEmbedder())))
    assert await tasks.run_task(sm, tid, deps, force=True) == "error"
    assert "AI_CONNECTION_UNAVAILABLE" in notifier.sent[0][1] and notifier.admin


async def test_collect_results_records_metrics(pg):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "К", slug="kk", default_tasks=False)
        src = await add_source(s, c.id, "telegram_web", "https://t.me/salstek")
        s.add(Document(company_id=c.id, source_id=src.id, source_type="telegram", external_key="telegram:salstek:501",
                       url="https://t.me/salstek/501", title="t", text="x", content_hash="h",
                       metrics={"views": 1234}, meta={"message_ids": [501], "channel_username": "salstek"}))
        p = await P.create_draft(s, c.id, text="x")
        p.status, p.external_message_id, p.target_chat_id = "published", "501", "@salstek"
        p.published_at = datetime.now(timezone.utc)
        t = await tasks.create_task(s, c.id, kind="collect_results", schedule={"every_minutes": 60})
        tid, cid = t.id, c.id
    assert await tasks.run_task(sm, tid, tasks.TaskDeps(notifier=LogNotifier()), force=True) == "ok"
    async with sm() as s:
        m = await s.scalar(select(Memory).where(Memory.company_id == cid, Memory.category == "analysis"))
        assert "views 1234" in m.content


async def test_vk_comments_collect_and_reply(pg):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "К", slug="kk", default_tasks=False)
        src = await add_source(s, c.id, "vk", "https://vk.com/club123")
        s.add(Document(company_id=c.id, source_id=src.id, source_type="vk", external_key="vk:123:55", url="u",
                       title="t", text="x", content_hash="h", published_at=datetime.now(timezone.utc)))
        await secrets.set_secret(s, c.id, "vk_token", "vk1.a.t")
        cid = c.id
    calls = []
    transport = httpx.MockTransport(vk_handler(calls))
    res = await inbox.collect_vk_comments(sm, cid, transport=transport)
    assert res["new"] == 1
    assert (await inbox.collect_vk_comments(sm, cid, transport=transport))["new"] == 0  # без дублей
    async with sm() as s:
        item = await s.scalar(select(InboxItem))
        assert item.author_name == "Иван П" and item.post_ref == "-123_55"
        item.reply_text = "Цена зависит от размера, напишите нам"
        await inbox.send_reply(s, item, user_id=None, transport=transport)
        await s.commit()
        assert item.status == "replied" and item.reply_external_id == "3"
    form = calls[-1][1]
    assert calls[-1][0] == "wall.createComment" and form["reply_to_comment"] == "1" and form["from_group"] == "123"


async def test_auto_reply_requires_permission(pg):
    _, sm = pg
    async with sm() as s, s.begin():
        c = await create_company(s, "К", slug="kk", default_tasks=False)
        await inbox.upsert_item(s, c.id, platform="telegram", chat_ref="-1", external_id="5", text="?")
        cid = c.id
    deps = tasks.TaskDeps(notifier=LogNotifier(), orchestrator=Orchestrator(MockConnector(), HybridSearcher(HashEmbedder())))
    assert await inbox.auto_reply(sm, cid, deps) == 0  # разрешения нет — ничего не отправлено
    async with sm() as s:
        assert (await s.scalar(select(InboxItem))).status == "new"


# --------------------------------------------------------------------------- ChatGPT: повтор при 503

async def test_chatgpt_retries_503(tmp_path):
    from cryptography.fernet import Fernet

    from app.ai.auth.token_refresh import TokenManager
    from app.ai.auth.token_storage import ChatGPTCredentials, EncryptedTokenStorage
    from app.ai.connectors.base import AIMessage
    from app.ai.connectors.chatgpt_plus import ChatGPTPlusConnector

    storage = EncryptedTokenStorage(tmp_path / "c.enc", Fernet(Fernet.generate_key()))
    storage.save(ChatGPTCredentials(client_id="oaiapp_x", ext_agent_host_id="urn:uuid:1", access_token="a",
                                    refresh_token="r", scopes=["chatgpt.tokens.use.direct"]))
    n = {"calls": 0}

    def handle(req):
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(503, json={"error": {"code": "subscription_sharing_usage_unavailable",
                                                       "message": "try later"}})
        sse = ('event: response.output_text.delta\ndata: {"delta": "ок"}\n\n'
               'event: response.completed\ndata: {"response": {"id": "r1", "model": "m", "status": "completed"}}\n\n')
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    conn = ChatGPTPlusConnector(TokenManager(storage), model="m", transport=httpx.MockTransport(handle), retry_base_s=0)
    res = await conn.complete([AIMessage("user", "привет")])
    assert res.text == "ок" and n["calls"] == 2
