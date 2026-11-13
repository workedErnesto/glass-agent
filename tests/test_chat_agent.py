"""Чат компании, память, агент и инструменты.

Модель — ТЕСТОВАЯ (MockConnector, ответы помечены is_mock=True). Эти тесты
проверяют логику backend, но НЕ подтверждают реальную интеграцию с ChatGPT Plus.
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai.agent import parse_action
from app.ai.connectors.base import AIConnectionUnavailable, MockConnector
from app.ai.orchestrator import Orchestrator
from app.db.models import ConversationSummary, Document, Memory, Message, Post
from app.rag.index import HybridSearcher, index_documents
from app.services import memory as mem
from app.services import posts as posts_svc
from app.services.sources import add_source, create_company

from .conftest import TEST_DB_URL, HashEmbedder


def j(**kw):
    return json.dumps(kw, ensure_ascii=False)


async def seed(sm):
    emb = HashEmbedder()
    async with sm() as s, s.begin():
        a = await create_company(s, "Салаватстекло", slug="a")
        b = await create_company(s, "Другая компания", slug="b")
        for comp, text in ((a, "Энергосберегающее стекло Ultraview для фасадов"),
                           (b, "Секретный прайс компании Б на кирпич")):
            src = await add_source(s, comp.id, "website", f"https://{comp.slug}.ru/")
            d = Document(company_id=comp.id, source_id=src.id, source_type="website", external_key=f"website:{comp.slug}:1",
                         url=f"https://{comp.slug}.ru/1", title=text[:30], text=text, content_hash=comp.slug)
            s.add(d)
            await s.flush()
            await index_documents(s, emb, [d])
    return a.id, b.id


def orch(mock, **kw):
    return Orchestrator(mock, HybridSearcher(HashEmbedder()), **kw)


def test_parse_action():
    assert parse_action('```json\n{"tool": "x", "args": {}}\n```') == {"tool": "x", "args": {}}
    assert parse_action('Думаю... {"answer": "ок"} конец')["answer"] == "ок"
    assert parse_action("просто текст") is None


async def test_agent_tools_post_memory_and_isolation(pg):
    _, sm = pg
    a, b = await seed(sm)
    mock = MockConnector([
        j(tool="search_company_knowledge", args={"query": "стекло прайс кирпич", "company_id": str(b)}),
        j(tool="create_post_draft", args={"topic": "Энергосбережение"}),
        "Текст поста про энергосберегающее стекло",  # ответ модели внутри инструмента
        j(answer="Черновик готов", remember=[{"category": "brand_style", "content": "Пишем без канцелярита"}],
          current_task="Пост про энергосбережение"),
    ])
    async with sm() as s:
        r = await orch(mock).chat(s, a, "Сделай пост про энергосбережение", user_id=None)
    assert r.answer == "Черновик готов" and r.is_mock and r.provider == "mock"
    assert [st["tool"] for st in r.steps] == ["search_company_knowledge", "create_post_draft"]
    assert all(st["ok"] for st in r.steps)
    # результат поиска передан модели и не содержит данных компании Б, несмотря на company_id в аргументах
    tool_msg = next(m.content for m in mock.calls[1]["messages"] if m.content.startswith("Результат инструмента"))
    assert "Ultraview" in tool_msg and "Секретный" not in tool_msg
    async with sm() as s:
        post = await s.scalar(select(Post).where(Post.company_id == a))
        assert post.status == "draft" and post.generated_by["is_mock"] is True
        assert await s.scalar(select(func.count()).select_from(Post).where(Post.company_id == b)) == 0
        lt = (await s.scalars(select(Memory).where(Memory.company_id == a, Memory.kind == "long_term"))).all()
        assert [m.content for m in lt] == ["Пишем без канцелярита"]
        ep = (await s.scalars(select(Memory).where(Memory.company_id == a, Memory.kind == "episodic"))).all()
        assert any(m.category == "post_created" for m in ep)
        msgs = (await s.scalars(select(Message).where(Message.company_id == a).order_by(Message.id))).all()
        assert [m.role for m in msgs] == ["user", "assistant"] and msgs[1].is_mock
        assert await s.scalar(select(func.count()).select_from(Message).where(Message.company_id == b)) == 0
    # контекст первой модели содержит RAG-фрагмент своей компании и ни одного чужого
    instr = mock.calls[0]["instructions"]
    assert "Ultraview" in instr and "кирпич" not in instr


async def test_history_and_memory_survive_restart(pg):
    _, sm = pg
    a, b = await seed(sm)
    async with sm() as s:
        await orch(MockConnector([j(answer="Запомнил", remember=[{"category": "goal", "content": "Цель: 100 заявок"}])])
                   ).chat(s, a, "Наша цель — 100 заявок в месяц")
        await orch(MockConnector([j(answer="Привет Б")])).chat(s, b, "Сообщение компании Б")

    # «перезапуск»: новый движок и новые объекты
    engine2 = create_async_engine(TEST_DB_URL)
    sm2 = async_sessionmaker(engine2, expire_on_commit=False)
    try:
        mock = MockConnector([j(answer="Помню цель")])
        async with sm2() as s:
            hist = await mem.history(s, a)
            assert [m.content for m in hist] == ["Наша цель — 100 заявок в месяц", "Запомнил"]
            await orch(mock).chat(s, a, "Какая у нас цель?")
        sent = [m.content for m in mock.calls[0]["messages"]]
        assert sent == ["Наша цель — 100 заявок в месяц", "Запомнил", "Какая у нас цель?"]
        assert "Цель: 100 заявок" in mock.calls[0]["instructions"]
        assert "Сообщение компании Б" not in " ".join(sent) + mock.calls[0]["instructions"]
    finally:
        await engine2.dispose()


async def test_ai_unavailable_keeps_user_message(pg):
    _, sm = pg
    a, _ = await seed(sm)

    class Down(MockConnector):
        async def complete(self, *a, **k):
            raise AIConnectionUnavailable("нет подключения", reason="not_connected")

    async with sm() as s:
        with pytest.raises(AIConnectionUnavailable) as ei:
            await orch(Down()).chat(s, a, "Привет")
        assert ei.value.code == "AI_CONNECTION_UNAVAILABLE"
    async with sm() as s:
        roles = (await s.scalars(select(Message.role).where(Message.company_id == a))).all()
        assert roles == ["user"]


async def test_budget_limits_agent(pg):
    _, sm = pg
    a, _ = await seed(sm)
    mock = MockConnector([j(tool="get_company_profile", args={})] * 10)
    async with sm() as s:
        r = await orch(mock, max_llm_calls=3).chat(s, a, "Зациклись")
    assert r.stopped_reason == "budget" and r.llm_calls == 3 and len(mock.calls) == 3
    assert "Остановлено" in r.answer


async def test_max_steps_forces_final_answer(pg):
    _, sm = pg
    a, _ = await seed(sm)
    mock = MockConnector([j(tool="get_company_memory", args={})] * 2 + [j(answer="Итог по данным")])
    async with sm() as s:
        r = await orch(mock, max_steps=2).chat(s, a, "Анализ")
    assert r.answer == "Итог по данным" and r.stopped_reason == "max_steps" and len(r.steps) == 2


async def test_unknown_tool_and_tool_error_are_reported(pg):
    _, sm = pg
    a, _ = await seed(sm)
    mock = MockConnector([j(tool="delete_everything", args={}), j(tool="update_post_draft", args={"post_id": "nope"}),
                          j(answer="Не получилось")])
    async with sm() as s:
        r = await orch(mock).chat(s, a, "x")
    assert [st["ok"] for st in r.steps] == [False, False]


async def test_summaries_keep_original_messages(pg):
    _, sm = pg
    a, _ = await seed(sm)
    cfg = mem.MemoryConfig(recent_messages=4, summarize_batch=4)
    async with sm() as s:
        for i in range(4):
            await orch(MockConnector([j(answer=f"ответ {i}"), "Сводка: обсуждали тему"]), memory_cfg=cfg
                       ).chat(s, a, f"вопрос {i}")
    async with sm() as s:
        sums = (await s.scalars(select(ConversationSummary).where(ConversationSummary.company_id == a))).all()
        assert len(sums) >= 1 and sums[0].summary.startswith("[ТЕСТОВАЯ МОДЕЛЬ]")
        assert await s.scalar(select(func.count()).select_from(Message).where(Message.company_id == a)) == 8
        loaded = await mem.load_memory(s, a)
        assert loaded["summaries"]


async def test_post_approval_workflow(pg):
    _, sm = pg
    a, _ = await seed(sm)
    async with sm() as s, s.begin():
        p = await posts_svc.create_draft(s, a, text="Первый текст", topic="t")
        with pytest.raises(posts_svc.PostStateError):
            await posts_svc.schedule(s, p, None)  # без согласования нельзя
        await posts_svc.submit(s, p)
        await posts_svc.reject(s, p, user_id=None, comment="длинно")
        assert p.status == "rejected"
        await posts_svc.edit_content(s, p, text="Короче")
        assert p.status == "draft" and p.revision == 2
        await posts_svc.approve(s, p, user_id=None)
        assert p.status == "approved" and posts_svc.is_approved_content(p)
        await posts_svc.schedule(s, p, None)
        assert p.status == "scheduled"
        # существенное изменение после согласования → снова черновик, нужна повторная проверка
        await posts_svc.edit_content(s, p, text="Совсем другой текст")
        assert p.status == "draft" and p.approved_hash is None and p.scheduled_at is None
        with pytest.raises(posts_svc.PostStateError):
            await posts_svc.schedule(s, p, None)
