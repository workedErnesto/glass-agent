"""Telegram-бот Glass Agent — основной интерфейс клиента и администратора (п. 3.1 ТЗ). Long polling.

Клиент:   любой текст — поручение AI-маркетологу выбранной компании; /posts — на согласовании;
          кнопки под постом: согласовать / опубликовать сейчас / на доработку (с комментарием) / отклонить;
          /report — отчёт; /tasks — регулярные задачи; /inbox — комментарии; /ai — статус подключения ChatGPT.
Админ:    /newcompany Название — подключить компанию; /addsource <тип> <ссылка> [конкурент];
          /setchannel <telegram|vk|max|telegram_discussion> <id>; /invite <telegram_id> <роль>; /sync.
Группа обсуждения канала: сообщения попадают во входящие компании (комментарии).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.connectors.base import AIError
from app.ai.orchestrator import Orchestrator
from app.core.config import get_settings
from app.db.models import Company, CompanyMember, InboxItem, ScheduledTask, User
from app.services import analytics, inbox as inbox_svc, posts as P
from app.services.publisher import PublishError, TelegramBotAPI, approval_message

log = logging.getLogger(__name__)
ROLE_RANK = {"viewer": 0, "editor": 1, "approver": 2, "owner": 3}
HELP = ("Glass Agent — AI-маркетолог.\n\n"
        "Пишите задачу обычным текстом: «подготовь пост про …», «что у нас с охватами?», «составь план на неделю».\n\n"
        "/companies — мои компании · /use <slug> — выбрать\n/posts — на согласовании · /report — отчёт\n"
        "/tasks — регулярные задачи · /inbox — комментарии · /ai — статус ChatGPT")
ADMIN_HELP = ("\n\nАдминистратор:\n/newcompany Название\n/addsource website|telegram_web|vk <ссылка> [конкурент]\n"
              "/setchannel telegram|vk|max|telegram_discussion <id>\n/invite <telegram_id> owner|approver|editor|viewer\n"
              "/sync — обновить источники")


class GlassBot:
    def __init__(self, api: TelegramBotAPI, sm: async_sessionmaker[AsyncSession], orchestrator: Orchestrator | None,
                 *, task_deps: Any = None):
        self.api = api
        self.sm = sm
        self.orchestrator = orchestrator
        self.task_deps = task_deps
        self.offset = 0

    # ------------------------------------------------------------------ общее

    async def _user(self, session: AsyncSession, tg: dict[str, Any]) -> User:
        s = get_settings()
        u = await session.scalar(select(User).where(User.telegram_id == tg["id"]))
        is_admin = tg["id"] in s.admin_ids
        name = (" ".join(x for x in (tg.get("first_name"), tg.get("last_name")) if x) or tg.get("username") or "")[:200]
        if u is None:
            u = User(telegram_id=tg["id"], name=name, is_admin=is_admin, prefs={})
            session.add(u)
            await session.commit()
        elif u.is_admin != is_admin or (name and not u.name):
            u.is_admin, u.name = is_admin, u.name or name
            await session.commit()
        return u

    async def _role(self, session: AsyncSession, user: User, company_id: uuid.UUID) -> str | None:
        if user.is_admin:
            return "owner"
        return await session.scalar(select(CompanyMember.role).where(CompanyMember.company_id == company_id,
                                                                      CompanyMember.user_id == user.id))

    async def _companies(self, session: AsyncSession, user: User) -> list[tuple[Company, str]]:
        if user.is_admin:
            return [(c, "admin") for c in (await session.scalars(select(Company).order_by(Company.created_at))).all()]
        return list((await session.execute(select(Company, CompanyMember.role).join(
            CompanyMember, CompanyMember.company_id == Company.id).where(CompanyMember.user_id == user.id))).all())

    def _set_pref(self, user: User, **kw: Any) -> None:
        user.prefs = {**(user.prefs or {}), **kw}

    async def _selected(self, session: AsyncSession, user: User) -> Company | None:
        cid = (user.prefs or {}).get("company_id")
        rows = await self._companies(session, user)
        by_id = {str(c.id): c for c, _ in rows}
        if cid in by_id:
            return by_id[cid]
        if len(rows) == 1:
            self._set_pref(user, company_id=str(rows[0][0].id))
            await session.commit()
            return rows[0][0]
        return None

    async def send(self, chat_id: int, text: str, **extra: Any) -> None:
        for i in range(0, max(len(text), 1), 4000):
            await self.api.call("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000] or "…", **extra})

    # ------------------------------------------------------------------ обработка

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            return await self.on_callback(update["callback_query"])
        msg = update.get("message")
        if not msg or "text" not in msg:
            return
        if msg.get("chat", {}).get("type") in ("group", "supergroup"):
            async with self.sm() as session:
                if await inbox_svc.record_telegram_message(session, msg):  # комментарий в группе обсуждения
                    await session.commit()
            return
        if msg.get("chat", {}).get("type") != "private":
            return
        chat_id, text, tg = msg["chat"]["id"], msg["text"].strip(), msg["from"]
        async with self.sm() as session:
            user = await self._user(session, tg)
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                return await self._help(chat_id, user, tg)
            if cmd == "/companies":
                rows = await self._companies(session, user)
                return await self.send(chat_id, "\n".join(f"• {c.slug} — {c.name} ({r})" for c, r in rows)
                                       or "Компаний нет. Попросите администратора подключить вашу компанию.")
            if cmd == "/use":
                rows = {c.slug: c for c, _ in await self._companies(session, user)}
                if arg not in rows:
                    return await self.send(chat_id, "Компания не найдена. /companies — список.")
                self._set_pref(user, company_id=str(rows[arg].id), awaiting=None)
                await session.commit()
                return await self.send(chat_id, f"Выбрана компания «{rows[arg].name}». Пишите задачу.")
            if cmd == "/ai":
                return await self._ai_status(chat_id, user)
            if cmd == "/newcompany":
                return await self._new_company(session, chat_id, user, arg)
            company = await self._selected(session, user)
            if company is None:
                return await self.send(chat_id, "Сначала выберите компанию: /use <slug> (/companies — список)")
            role = await self._role(session, user, company.id)
            if role is None:
                self._set_pref(user, company_id=None)
                await session.commit()
                return await self.send(chat_id, "Нет доступа к компании.")
            rank = ROLE_RANK.get(role, 0)
            awaiting = (user.prefs or {}).get("awaiting")
            if awaiting and not cmd:
                return await self._awaited_input(session, chat_id, user, company, awaiting, text)
            if cmd == "/posts":
                return await self.show_pending(session, chat_id, company.id)
            if cmd == "/report":
                rep = await analytics.period_report(session, company.id, days=int(arg) if arg.isdigit() else 7)
                return await self.send(chat_id, analytics.report_text(company.name, rep))
            if cmd == "/tasks":
                return await self._tasks(session, chat_id, company.id)
            if cmd == "/inbox":
                return await self._inbox(session, chat_id, company.id)
            if cmd in ("/addsource", "/setchannel", "/invite", "/sync"):
                if rank < ROLE_RANK["owner"]:
                    return await self.send(chat_id, "Нужна роль owner.")
                return await self._admin_cmd(session, chat_id, user, company, cmd, arg)
            if cmd:
                return await self.send(chat_id, "Неизвестная команда. /help")
            if rank < ROLE_RANK["editor"]:
                return await self.send(chat_id, "Для поручений AI нужна роль editor.")
            await self._chat(session, chat_id, user, company, text)

    async def _help(self, chat_id: int, user: User, tg: dict[str, Any]) -> None:
        s = get_settings()
        extra = {}
        if s.mini_app_url:
            extra["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть приложение",
                                                           "web_app": {"url": s.mini_app_url}}]]}
        await self.send(chat_id, f"{HELP}{ADMIN_HELP if user.is_admin else ''}\n\nВаш Telegram ID: {tg['id']}", **extra)

    async def _ai_status(self, chat_id: int, user: User) -> None:
        if self.orchestrator is None:
            return await self.send(chat_id, "AI не настроен.")
        st = await self.orchestrator.connector.status()
        if st.get("is_mock"):
            text = "⚠️ Включена ТЕСТОВАЯ модель (AI_PROVIDER=mock_dev) — ответы не от ChatGPT."
        elif st.get("connected") and st.get("plan_usage_permission"):
            text = (f"✅ ChatGPT Plus подключён (Sign in with ChatGPT), разрешение на подписку есть."
                    f"{' Аккаунт: ' + str(st.get('email')) if user.is_admin and st.get('email') else ''}"
                    "\nОграничения: общее 5-часовое окно лимита Plus; без генерации изображений и файлов.")
        else:
            text = (f"❌ ChatGPT не подключён ({st.get('reason')}). AI-ответы недоступны: AI_CONNECTION_UNAVAILABLE.\n"
                    "Администратор: python -m scripts.chatgpt_login")
        await self.send(chat_id, text)

    async def _chat(self, session: AsyncSession, chat_id: int, user: User, company: Company, text: str) -> None:
        if self.orchestrator is None:
            return await self.send(chat_id, "AI не настроен.")
        await self.api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        try:
            r = await self.orchestrator.chat(session, company.id, text, user_id=user.id)
        except AIError as e:
            return await self.send(chat_id, f"{e.code}: {e}")
        answer = r.answer
        if r.is_mock and not answer.startswith("[ТЕСТОВАЯ МОДЕЛЬ]"):
            answer = "[ТЕСТОВАЯ МОДЕЛЬ] " + answer
        await self.send(chat_id, answer)

    async def _new_company(self, session: AsyncSession, chat_id: int, user: User, name: str) -> None:
        from app.services.sources import create_company, slugify

        if not user.is_admin:
            return await self.send(chat_id, "Подключать компании может администратор.")
        if not name:
            return await self.send(chat_id, "Формат: /newcompany Название компании")
        slug = slugify(name)
        if await session.scalar(select(Company.id).where(Company.slug == slug)):
            slug = f"{slug[:50]}-{uuid.uuid4().hex[:4]}"
        c = await create_company(session, name, slug=slug, user_id=user.id)
        session.add(CompanyMember(company_id=c.id, user_id=user.id, role="owner"))
        self._set_pref(user, company_id=str(c.id))
        await session.commit()
        await self.send(chat_id, f"Компания «{name}» подключена (slug {slug}) и выбрана.\nДальше:\n"
                                 "/addsource website https://сайт\n/addsource telegram_web https://t.me/канал\n"
                                 "/setchannel telegram @канал_для_публикаций\n/invite <telegram_id> owner\n"
                                 "Документы о компании можно отправить сюда файлом (PDF/DOCX/TXT) — скоро; "
                                 "сейчас через приложение: POST /api/companies/{id}/files")

    async def _admin_cmd(self, session: AsyncSession, chat_id: int, user: User, company: Company, cmd: str,
                         arg: str) -> None:
        from app.services.sources import SOURCE_TYPES, add_source

        parts = arg.split()
        if cmd == "/addsource":
            if len(parts) < 2 or parts[0] not in SOURCE_TYPES - {"file"}:
                return await self.send(chat_id, "Формат: /addsource website|telegram_web|telegram|vk <ссылка> [конкурент]")
            src = await add_source(session, company.id, parts[0], parts[1], user_id=user.id,
                                   is_competitor=len(parts) > 2 and parts[2].lower().startswith(("конк", "comp")))
            await session.commit()
            return await self.send(chat_id, f"Источник добавлен ({src.type}{', конкурент' if src.is_competitor else ''})."
                                            " /sync — загрузить сейчас (или ночью автоматически).")
        if cmd == "/setchannel":
            if len(parts) != 2 or parts[0] not in ("telegram", "vk", "max", "telegram_discussion"):
                return await self.send(chat_id, "Формат: /setchannel telegram|vk|max|telegram_discussion <id или @имя>")
            settings = dict(company.settings or {})
            settings["channels"] = {**(settings.get("channels") or {}), parts[0]: parts[1]}
            company.settings = settings
            await session.commit()
            return await self.send(chat_id, f"Канал {parts[0]}: {parts[1]}. Бот должен быть администратором канала.")
        if cmd == "/invite":
            if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or parts[1] not in ROLE_RANK:
                return await self.send(chat_id, "Формат: /invite <telegram_id> owner|approver|editor|viewer\n"
                                                "Telegram ID человек узнаёт командой /start у бота.")
            u = await session.scalar(select(User).where(User.telegram_id == int(parts[0])))
            if u is None:
                u = User(telegram_id=int(parts[0]), prefs={})
                session.add(u)
                await session.flush()
            m = await session.get(CompanyMember, (company.id, u.id))
            if m is None:
                session.add(CompanyMember(company_id=company.id, user_id=u.id, role=parts[1]))
            else:
                m.role = parts[1]
            await session.commit()
            return await self.send(chat_id, f"Доступ выдан: {parts[0]} — {parts[1]}")
        if cmd == "/sync":
            if self.task_deps is None or self.task_deps.embedder is None:
                return await self.send(chat_id, "Синхронизация из бота не настроена")
            from app.services.tasks import _run_sync

            await self.send(chat_id, "Обновляю источники, это может занять несколько минут…")
            task = ScheduledTask(company_id=company.id, kind="sync_sources", params={}, title="sync")
            _, text = await _run_sync(self.sm, task, self.task_deps)
            return await self.send(chat_id, text)

    async def _tasks(self, session: AsyncSession, chat_id: int, company_id: uuid.UUID) -> None:
        rows = (await session.scalars(select(ScheduledTask).where(ScheduledTask.company_id == company_id)
                                      .order_by(ScheduledTask.created_at))).all()
        if not rows:
            return await self.send(chat_id, "Регулярных задач нет.")
        lines = []
        for t in rows:
            nxt = t.next_run_at.astimezone().strftime("%d.%m %H:%M") if t.next_run_at and t.enabled else "—"
            lines.append(f"{'🟢' if t.enabled else '⚪️'} {t.title} · следующий запуск {nxt}"
                         f"{' · последний: ' + t.last_status if t.last_status else ''}")
        await self.send(chat_id, "Регулярные задачи:\n" + "\n".join(lines))

    async def _inbox(self, session: AsyncSession, chat_id: int, company_id: uuid.UUID) -> None:
        items = (await session.scalars(select(InboxItem).where(
            InboxItem.company_id == company_id, InboxItem.status.in_(["new", "draft_ready"]))
            .order_by(InboxItem.received_at.desc()).limit(5))).all()
        if not items:
            return await self.send(chat_id, "Новых комментариев нет.")
        for i in items:
            reply = f"\n\nЧерновик ответа:\n{i.reply_text}" if i.reply_text else ""
            buttons = [[{"text": "🤖 Черновик ответа", "callback_data": f"id:{i.id}"}]]
            if i.reply_text:
                buttons[0].append({"text": "📤 Отправить", "callback_data": f"is:{i.id}"})
            buttons.append([{"text": "Пропустить", "callback_data": f"ii:{i.id}"}])
            await self.send(chat_id, f"💬 {i.platform} · {i.author_name or 'пользователь'}:\n{i.text}{reply}",
                            reply_markup={"inline_keyboard": buttons})

    async def _awaited_input(self, session: AsyncSession, chat_id: int, user: User, company: Company,
                             awaiting: dict[str, Any], text: str) -> None:
        """Комментарий для «На доработку»: пост → revision, AI переписывает и отправляет повторно (если доступен)."""
        self._set_pref(user, awaiting=None)
        if awaiting.get("kind") != "revision":
            await session.commit()
            return
        try:
            post = await P.get_post(session, company.id, awaiting["post_id"])
            await P.request_changes(session, post, user_id=user.id, comment=text)
            await session.commit()
        except (LookupError, P.PostStateError) as e:
            await session.commit()
            return await self.send(chat_id, f"Не получилось: {e}")
        await self.send(chat_id, "Вернул на доработку. Переписываю по комментарию…")
        if self.orchestrator is None:
            return
        try:
            r = await self.orchestrator.chat(session, company.id,
                                             f"Доработай пост {post.id} по комментарию согласующего: «{text}». "
                                             "Затем отправь его на повторное согласование.", user_id=user.id,
                                             meta={"source": "revision"})
            await self.send(chat_id, r.answer)
        except AIError as e:
            await self.send(chat_id, f"AI недоступен ({e.code}) — отредактируйте пост в приложении и отправьте снова.")

    async def show_pending(self, session: AsyncSession, chat_id: int, company_id: uuid.UUID) -> None:
        posts = await P.list_posts(session, company_id, status=P.PENDING, limit=10)
        drafts = await P.list_posts(session, company_id, status=P.DRAFT, limit=5)
        if not posts and not drafts:
            return await self.send(chat_id, "Нет постов на согласовании и черновиков.")
        for p in posts:
            text, kb = approval_message(p)
            await self.send(chat_id, text, reply_markup=kb)
        for p in drafts:
            await self.send(chat_id, f"Черновик · {p.platform} · {p.id}\n\n{p.text[:3500]}", reply_markup={
                "inline_keyboard": [[{"text": "📨 На согласование", "callback_data": f"sb:{p.id}"}]]})

    async def on_callback(self, cq: dict[str, Any]) -> None:
        data, tg = cq.get("data", ""), cq["from"]
        action, _, oid = data.partition(":")
        note = "Готово"
        chat_id = (cq.get("message") or {}).get("chat", {}).get("id") or tg["id"]
        followup: str | None = None
        async with self.sm() as session:
            user = await self._user(session, tg)
            try:
                obj_id = uuid.UUID(oid)
            except ValueError:
                obj_id = None
            if action in ("id", "is", "ii"):
                note = await self._inbox_action(session, user, action, obj_id)
            else:
                post = await session.get(P.Post, obj_id) if obj_id else None
                need = ROLE_RANK["editor"] if action == "sb" else ROLE_RANK["approver"]
                if post is None:
                    note = "Пост не найден"
                elif ROLE_RANK.get(await self._role(session, user, post.company_id) or "", -1) < need:
                    note = "Недостаточно прав"
                else:
                    try:
                        if action == "ap":
                            await P.approve(session, post, user_id=user.id, scheduled_at=post.proposed_at)
                            note = ("Согласовано, публикация " + post.proposed_at.astimezone().strftime("%d.%m %H:%M")
                                    if post.proposed_at else "Согласовано. Время публикации — в приложении или «Опубликовать»")
                        elif action == "pn":
                            await P.approve(session, post, user_id=user.id, scheduled_at=datetime.now(timezone.utc))
                            note = "Согласовано, публикация в ближайшую минуту"
                        elif action == "rj":
                            await P.reject(session, post, user_id=user.id, comment="Отклонено в боте")
                            note = "Отклонено — публикации не будет"
                        elif action == "rv":
                            self._set_pref(user, awaiting={"kind": "revision", "post_id": str(post.id)},
                                           company_id=str(post.company_id))
                            note = "Напишите комментарий — что доработать"
                            followup = "✏️ Напишите одним сообщением, что доработать в посте:"
                        elif action == "sb":
                            await P.submit(session, post, user_id=user.id)
                            note = "Отправлено на согласование"
                        await session.commit()
                    except P.PostStateError as e:
                        await session.rollback()
                        note = str(e)
        await self.api.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note[:190]})
        if followup:
            await self.send(chat_id, followup)

    async def _inbox_action(self, session: AsyncSession, user: User, action: str, item_id: uuid.UUID | None) -> str:
        item = await session.get(InboxItem, item_id) if item_id else None
        if item is None:
            return "Сообщение не найдено"
        rank = ROLE_RANK.get(await self._role(session, user, item.company_id) or "", -1)
        if action == "ii" and rank >= ROLE_RANK["editor"]:
            item.status = inbox_svc.IGNORED
            await session.commit()
            return "Пропущено"
        if action == "id" and rank >= ROLE_RANK["editor"]:
            if self.orchestrator is None:
                return "AI не настроен"
            try:
                await inbox_svc.draft_reply(session, item, self.orchestrator.connector, self.orchestrator.searcher)
                await session.commit()
            except AIError as e:
                await session.rollback()
                return f"AI недоступен: {e.code}"
            return "Черновик готов — /inbox"
        if action == "is" and rank >= ROLE_RANK["approver"]:
            try:
                await inbox_svc.send_reply(session, item, user_id=user.id, telegram_api=self.api)
            except (ValueError, PublishError) as e:
                await session.commit()
                return f"Не отправлено: {e}"
            await session.commit()
            return "Ответ отправлен"
        return "Недостаточно прав"

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        log.info("Бот запущен (long polling)")
        while not stop.is_set():
            try:
                updates = await self.api.call("getUpdates", {"offset": self.offset, "timeout": 50,
                                                             "allowed_updates": ["message", "callback_query"]},
                                              timeout=60)
            except PublishError as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                try:
                    await self.handle(u)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка обработки update %s", u.get("update_id"))
