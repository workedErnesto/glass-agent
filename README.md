# Glass Agent — backend AI-маркетолога

Автономный AI-маркетолог для нескольких компаний: Telegram-бот и REST API для Mini App, база знаний компании
(сайт, каналы, документы), агент с инструментами, согласование и публикация в Telegram / VK / MAX, регулярные
задачи и отчёты. Модель — **только ChatGPT Plus через официальный Sign in with ChatGPT**, без платного OpenAI API.

Требования: [GLASS_AGENT_CODEX_PROMPT.md](GLASS_AGENT_CODEX_PROMPT.md), ТЗ хакатона `doc-1794511527.pdf`.

## Документация

| Документ | Для кого |
|---|---|
| [docs/HANDOFF.md](docs/HANDOFF.md) | участнику проекта: что готово, как подключить ChatGPT/VK/MAX, карта кода, как расширять |
| [docs/FRONTEND.md](docs/FRONTEND.md) + [docs/openapi.json](docs/openapi.json) | разработчику Mini App (Vue 3) |
| [docs/NEW_COMPANY.md](docs/NEW_COMPANY.md) | инструкция по подключению новой компании |
| [docs/FEATURES_AND_LIMITS.md](docs/FEATURES_AND_LIMITS.md) | функции и ограничения, соответствие критериям приёмки ТЗ |
| [docs/CHATGPT_PLUS_OAUTH.md](docs/CHATGPT_PLUS_OAUTH.md) | проверка способа подключения ChatGPT Plus |
| [docs/COSTS.md](docs/COSTS.md) | оценка месячных затрат |
| [docs/ROADMAP.md](docs/ROADMAP.md) | доработки для коммерческого использования |
| [docs/DEPLOY.md](docs/DEPLOY.md) | развёртывание 24/7 на VM |
| [docs/DEMO.md](docs/DEMO.md) | сценарий демонстрации |

## Быстрый старт

```bash
make install && make db && make migrate
make test                                   # 101 тест
.venv/bin/python -m scripts.doctor          # что настроено и как исправить
.venv/bin/python -m scripts.chatgpt_login   # подключить ChatGPT Plus (браузер)
make api        # REST API :8000, /docs
make bot        # Telegram-бот
make scheduler  # публикации по расписанию и регулярные задачи
make dev-mock   # API с ТЕСТОВОЙ моделью и входом без initData — для разработки фронтенда
```

Docker (24/7): `docker compose up -d db && docker compose --profile app up -d --build`.

## Статус

| Часть | Состояние |
|---|---|
| Подключение ChatGPT Plus (OAuth + PKCE, токены, лимиты, 503-повтор) | ✅ код по документации; ⏳ реальный вход — `scripts.chatgpt_login` |
| Компании, роли, секреты, журнал, расход AI, миграции | ✅ |
| Постоянный чат компании, память (4 вида), сводки | ✅ |
| Агент: 19 инструментов, лимиты шагов/вызовов/времени/дня | ✅ на тестовой модели |
| Парсеры: сайт 🟢, Telegram 🟢, VK, PDF/DOCX/TXT | ✅ (🟢 — проверено на реальных данных) |
| RAG: локальные эмбеддинги, гибридный поиск, изоляция | ✅ 🟢 |
| Посты: проверки, согласование (одобрить / доработка / отклонить), публикация | ✅ Telegram; VK и MAX — по документации, на моках |
| Регулярные задачи: отчёты, обновление знаний, результаты, комментарии, поручения агенту | ✅ |
| Комментарии и ответы (автоответ — по разрешению) | ✅ Telegram, VK; MAX — нет |
| Telegram-бот (клиент и настройка компаний без кода) | ✅ |
| REST API + initData + OpenAPI | ✅ |
| Vue 3 Mini App | ❌ — [docs/FRONTEND.md](docs/FRONTEND.md) |

Не проверено вживую (нет доступа): ответы ChatGPT Plus, публикация в реальный канал, VK, MAX.
Автотесты используют моки сетей и **тестовую модель**, помеченную `is_mock=true` / «[ТЕСТОВАЯ МОДЕЛЬ]»,
и не обращаются к внешним сервисам.

## Парсеры (CLI)

```bash
.venv/bin/python -m scripts.manage add-source salavatsteklo website https://salstek.ru/ --options '{"max_pages": 400}'
.venv/bin/python -m scripts.manage add-source salavatsteklo telegram_web https://t.me/salstek
.venv/bin/python -m scripts.manage sync salavatsteklo
.venv/bin/python -m scripts.manage search salavatsteklo "светопропускание Ultraview R"
.venv/bin/python -m scripts.telegram_watch            # опрос открытого канала без ключей
.venv/bin/python -m scripts.parse_sources --company salavatsteklo telegram-export ~/Downloads/ChatExport   # закрытый канал
```

* Сайт: httpx + BeautifulSoup + trafilatura, sitemap, robots.txt, SSRF-защита, лимиты, повторная синхронизация.
* Telegram: открытый канал — веб-превью `t.me/s/…` без ключей; закрытый — экспорт Telegram Desktop;
  Telethon (`TG_API_ID/HASH`) и Bot API — для доступной истории и новых постов.
* VK: `groups.getById`, `wall.get`; ключ — `python -m scripts.vk_token` (нужен `VK_APP_ID`).
* Документы: PDF / DOCX / TXT / MD — загрузка через API или источник `file`.
