# Передача проекта: что готово и что сделать участнику

Документ для участника, который подключает свой ChatGPT Plus, VK и MAX, пишет фронтенд (Vue 3 Mini App)
и дорабатывает backend. Требования: `GLASS_AGENT_CODEX_PROMPT.md` (промпт) и `doc-1794511527.pdf` (ТЗ хакатона).
Соответствие требованиям по пунктам — [FEATURES_AND_LIMITS.md](FEATURES_AND_LIMITS.md).

## 1. Состояние на момент передачи

| Часть | Состояние |
|---|---|
| Backend (FastAPI, PostgreSQL + pgvector, Alembic) | ✅ готов, 101 автотест |
| Парсеры: сайт, Telegram (веб-превью / экспорт / Telethon / Bot API), VK, PDF/DOCX/TXT | ✅ сайт и открытый Telegram проверены на реальных данных; VK — только на моках |
| RAG (локальные эмбеддинги e5-small, гибридный поиск RRF, изоляция компаний) | ✅ 583 документа Салаватстекло проиндексированы |
| Агент + 19 инструментов, память, сводки, лимиты | ✅ на тестовой модели; **с ChatGPT не проверен** |
| Подключение ChatGPT Plus (Sign in with ChatGPT, OAuth + PKCE) | ✅ код по документации OpenAI; **реальный вход не выполнялся** |
| Согласование и публикация Telegram | ✅ на моках; реальный бот @glass_uksivt_bot отвечает (`doctor --live`) |
| Публикация VK и MAX | ✅ по документации, на моках; **реальные аккаунты не проверены** |
| Регулярные задачи, отчёты, сбор результатов и комментариев | ✅ |
| Telegram-бот (клиент + админ) | ✅ на моках Bot API |
| REST API + initData + роли + OpenAPI | ✅ `docs/openapi.json` |
| Vue 3 Mini App | ❌ не начат — [FRONTEND.md](FRONTEND.md) |

## 2. Первый запуск (15 минут)

```bash
make install            # .venv, зависимости, .env из .env.example
make db                 # PostgreSQL + pgvector (Docker), порт 5433
make migrate            # миграции Alembic
make test               # 101 тест, нужна БД
python -m scripts.doctor          # что настроено, что нет — с командами исправления
```

Заполните в `.env`: `TG_BOT_TOKEN` (бот от @BotFather), `ADMIN_TELEGRAM_IDS` (свой Telegram ID — бот пишет
его на /start). `GLASS_SECRET_KEY` — ключ шифрования токенов (пусто = создастся `data/secrets/master.key`;
**не теряйте его**: без ключа сохранённые токены не расшифровать).

## 3. Подключить ChatGPT Plus (ваш аккаунт)

```bash
.venv/bin/python -m scripts.chatgpt_login          # откроется браузер: вход в ChatGPT и согласие
.venv/bin/python -m scripts.chatgpt_login --test   # список моделей + настоящий ответ модели
.venv/bin/python -m scripts.doctor --live
```

* Вход работает только когда браузер и backend на одной машине (redirect на `http://127.0.0.1:1455`).
  Для сервера: войти локально и перенести файл — [DEPLOY.md](DEPLOY.md).
* `CHATGPT_MODEL` пусто — берётся первая модель из `/v1/models` аккаунта.
* Подробности, ограничения подписки и почему облачный SaaS требует заявки в OpenAI — [CHATGPT_PLUS_OAUTH.md](CHATGPT_PLUS_OAUTH.md).
* `OPENAI_API_KEY` не используется нигде, это проверяет тест. Не добавляйте платный API.

**Что проверить сразу после входа** (код этих мест писался без доступа к модели):
1. `chatgpt_login --test` — модель отвечает.
2. В боте: «подготовь пост про энергосберегающее стекло» — агент должен вызвать `create_post_draft`,
   `prepare_post_image`, `submit_for_approval`. Если модель отвечает не JSON, разбор
   (`app/ai/agent.py: parse_action`) отдаст текст как финальный ответ — смотрите `meta.steps` сообщения.
3. Промпты агента — `app/ai/context_builder.py` (AGENT_RULES) и `app/ai/tools.py` (GEN_RULES, post_rules).
   Подстройка промптов под реальную модель — ожидаемая доработка.
4. Возможное улучшение: нативные function tools вместо JSON в тексте (документация разрешает функции в namespaces).

## 4. Подключить VK

1. Создать приложение VK: https://id.vk.com/about/business/go (тип «Сайт»/Standalone), `VK_APP_ID` в `.env`.
2. **Чтение** (парсер, комментарии): `python -m scripts.vk_token` — ключ по ссылке с правом `offline`.
3. **Публикация и ответы**: нужен ключ пользователя — администратора сообщества с правами `wall,photos,offline`:
   `VK_OAUTH_SCOPE=wall,photos,offline python -m scripts.vk_token`, затем сохранить как секрет компании
   `python -m scripts.manage secret <slug> vk_token` (или `VK_PUBLISH_TOKEN` для всех компаний).
4. Канал: `python -m scripts.manage settings <slug> channels.vk=club123456` (или в боте `/setchannel vk club123456`).
5. Проверить: `doctor --live`, источник `vk` → `manage sync`, тестовый пост с `platform=vk`.

Код: `app/parsers/vk_parser.py` (чтение), `app/services/platforms.py: VKPublisher` (wall.post + загрузка фото),
`app/services/inbox.py` (wall.getComments / wall.createComment). Документация dev.vk.com при подготовке была
недоступна для автоматической загрузки — **параметры методов сверить** при первом реальном запуске.

## 5. Подключить MAX

1. business.max.ru → Чат-боты → создать бота → Настройки → токен. `MAX_BOT_TOKEN` в `.env`
   (или секрет компании `max_bot_token`).
2. Добавить бота в канал (администратором), узнать `chat_id` канала, `channels.max=<chat_id>`.
3. `doctor --live` (GET /me), тестовый пост с `platform=max`.

Код: `app/services/platforms.py: MaxAPI, MaxPublisher` — по dev.max.ru/docs-api: база `platform-api2.max.ru`,
токен в заголовке `Authorization`, загрузка картинки `POST /uploads?type=image` → token → вложение,
повтор при `attachment.not.ready`. Не сделано для MAX: сбор комментариев и диалог с ботом (только публикация).

## 6. Карта кода

```
app/
  ai/            connectors/ (chatgpt_plus, dev_mock, base), auth/ (oauth, flow, token_*),
                 agent.py (цикл агента), tools.py (инструменты), context_builder.py, orchestrator.py (чат компании)
  api/           main.py (приложение, формат ошибок), routes.py (эндпоинты), deps.py (initData, роли), schemas.py
  bot/bot.py     Telegram-бот
  db/            models.py (все таблицы), session.py (подключение, миграции)
  parsers/       website, telegram_*, vk, file
  rag/           chunking, embeddings (e5), index (гибридный поиск)
  services/      posts (статусы и согласование), checks (проверка перед публикацией), publisher + platforms
                 (Telegram/VK/MAX), tasks (регулярные задачи), inbox (комментарии), analytics (статистика, отчёты),
                 memory, usage (расход AI), secrets (токены компаний), images (шаблоны Pillow), sources
scripts/         manage (CLI), doctor, chatgpt_login, vk_token, bot, scheduler, parse_sources, telegram_watch
migrations/      Alembic
tests/           pytest (нужна БД glass_test на порту 5433)
```

Процессы в продакшене: `api` (REST), `bot` (long polling), `scheduler` (публикации + задачи) —
`docker compose --profile app up -d --build`.

## 7. Как расширять

* **Новый инструмент агента** — функция с декоратором `@tool(...)` в `app/ai/tools.py`; компания берётся из
  `ctx.company_id` (никогда не из аргументов модели); вызовы модели — только через `ctx.llm()/ctx.complete()`
  (учёт расхода и лимиты).
* **Новая площадка** — класс с `async publish(post, target) -> {"message_id", "url", "chat_id"}` в
  `app/services/platforms.py`, ветка в `PublisherHub.resolve`, лимит текста в `services/checks.py`,
  значение в `posts.PLATFORMS`.
* **Новый вид регулярной задачи** — функция `_run_<kind>` и запись в `RUNNERS` в `app/services/tasks.py`.
* **Изменение схемы БД** — правка `app/db/models.py`, затем
  `.venv/bin/alembic revision --autogenerate -m "..."`, проверить файл в `migrations/versions/`, `make migrate`.
* **Новый эндпоинт** — `app/api/routes.py`, доступ через `Depends(access("<роль>"))`, схема ответа в `schemas.py`,
  затем `make openapi` для фронтенда.

## 8. Правила проекта (не нарушать)

* Только ChatGPT Plus через Sign in with ChatGPT. Никаких `OPENAI_API_KEY`, cookie, неофициальных endpoint'ов.
* Тестовая модель (`MockConnector`, `AI_PROVIDER=mock_dev`) всегда помечена `is_mock=true` и «[ТЕСТОВАЯ МОДЕЛЬ]».
* Публикация только после согласования; изменение текста, картинки или площадки снимает согласование.
* Автоответы на комментарии — только при `settings.permissions.auto_reply=true` компании.
* Данные компании — только по `company_id` из авторизованного запроса.
* Тесты не ходят во внешние сервисы: фикстура `no_real_credentials` обнуляет токены из `.env`.

## 9. Известные ограничения и что доработать первым

1. Проверить всё, что помечено «не проверено» в таблице раздела 1, на реальных аккаунтах.
2. Подстроить промпты агента под реальную модель (см. раздел 3).
3. MAX: сбор комментариев, диалог с ботом; VK: бот сообщества (сейчас клиентский интерфейс — Telegram).
4. Бот хранит выбор компании в `users.prefs`; загрузка документов файлом в боте пока не сделана (только API).
5. SSRF-защита парсера сайта проверяет IP до запроса; закрепление адреса на время соединения (DNS rebinding) — не сделано.
6. Полный список для коммерческой версии — [ROADMAP.md](ROADMAP.md).
