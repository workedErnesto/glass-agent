# Фронтенд: Telegram Mini App (Vue 3) — контракт с backend

Полная схема: [`docs/openapi.json`](openapi.json) (обновить: `make openapi`), интерактивно — `http://127.0.0.1:8000/docs`.
Типы для TypeScript: `npx openapi-typescript docs/openapi.json -o src/api/schema.d.ts`.

## Разработка без Telegram и без ChatGPT

```bash
make db && make migrate
make dev-mock          # API на :8000: ТЕСТОВАЯ модель (AI_PROVIDER=mock_dev) и вход без initData под Telegram ID 1 (админ)
```

* Запросы **без** заголовка `Authorization` авторизуются как `API_DEV_USER_ID` — только локально.
* Ответы тестовой модели начинаются с «[ТЕСТОВАЯ МОДЕЛЬ]», в ответах `is_mock: true` — показывайте плашку «тестовый режим».
* Сценарий «поручение → пост с картинкой → согласование» на тестовой модели проходит полностью.
* CORS: `CORS_ORIGINS=http://localhost:5173` в `.env` (или прокси dev-сервера Vite на `/api`).

## Авторизация

В Mini App: `Authorization: tma ${window.Telegram.WebApp.initData}` в каждом запросе. Backend проверяет подпись
(HMAC по токену бота) и срок (`INITDATA_MAX_AGE_S`, по умолчанию сутки). Пользователь создаётся автоматически.
`GET /api/me` → `{telegram_id, name, is_admin}`.

**Роли в компании:** `viewer` (смотреть) < `editor` (чат, посты, источники) < `approver` (согласование,
отправка ответов) < `owner` (настройки, участники, секреты, задачи). Администратор платформы (`is_admin`)
видит все компании и один может создавать компании и подключать ChatGPT. Чужая компания → 404.

## Ошибки

Всегда `{"error": {"code", "message", "reason"}}` (для 422 ещё `fields`).

| HTTP | code | Что показать |
|---|---|---|
| 401 | UNAUTHORIZED | открыть Mini App заново |
| 403 | FORBIDDEN | недостаточно прав |
| 404 | NOT_FOUND | |
| 409 | CONFLICT | недопустимый переход статуса / проверка поста не пройдена (`message`) |
| 422 | VALIDATION_ERROR | ошибки полей (`fields`) |
| 429 | AI_USAGE_LIMIT_EXCEEDED | исчерпан лимит ChatGPT Plus (окно 5 часов) |
| 429 | AI_COMPANY_QUOTA_EXCEEDED | дневной лимит AI компании |
| 503 | AI_CONNECTION_UNAVAILABLE | ChatGPT не подключён — AI-функции недоступны, остальное работает |
| 503 | AI_TEMPORARILY_UNAVAILABLE | повторить позже |

## Справочники

`GET /api/meta` — статусы постов, площадки, роли, виды задач, типы источников, имена секретов, шаблоны картинок.

## Экраны → эндпоинты

**Список компаний / создание (админ).** `GET /api/companies`, `POST /api/companies {name, slug?, profile, settings, owner_telegram_id}`.

**Настройки компании (owner).** `GET/PATCH /api/companies/{id}` — поля `profile` (описание, аудитория, УТП) и `settings`:
```json
{
  "channels": {"telegram": "@channel", "vk": "club123", "max": "-7001", "telegram_discussion": "-100500"},
  "brand": {"tone": "экспертно, без канцелярита", "banned_words": [], "required_words": [],
            "allowed_domains": ["salstek.ru"], "max_hashtags": 5, "require_image": false},
  "brand_colors": ["#0B3D5C", "#1F7A8C"], "logo": "logo.png", "site": "salstek.ru",
  "permissions": {"auto_reply": false},
  "ai_daily_call_limit": 300
}
```
Участники: `GET/POST /members`, `DELETE /members/{telegram_id}`. Токены соцсетей: `GET /secrets` (только имена и
последние символы), `PUT /secrets/{name} {value}`, `DELETE`. Журнал: `GET /audit`. Расход AI: `GET /usage`.

**Чат с AI-маркетологом.** `POST /chat {message}` → `{answer, steps[], citations[], created_post_ids[], is_mock, …}`
(ответ может идти 10–60 с: покажите индикатор; `steps` — какие инструменты вызвал агент).
История: `GET /chat/history?limit=50&before_id=…` (листание вверх). Память: `GET/POST /chat/memory`, `DELETE /chat/memory/{id}`.

**База знаний.** `GET/POST/DELETE /sources` (`{type, ref, is_competitor, sync_now}`), `POST /sources/{id}/sync`
(202, затем опрашивайте `GET /sources`: `status` syncing → ok | error, `last_result`). Документы:
`POST /files` (multipart `file`), `GET /files`, `DELETE /files/{name}`. Поиск: `POST /knowledge/search {query, scope}`.

**Посты.** Жизненный цикл:
```
draft → pending_approval → approved → scheduled → publishing → published
            ├→ revision (на доработку с комментарием) → draft
            └→ rejected                                   failed (ошибка публикации)
```
* Создать: `POST /posts/generate {topic, platform, image, proposed_at, submit}` (AI) или `POST /posts {text, platform}` (вручную).
* Список/карточка: `GET /posts?status=…`, `GET /posts/{id}`. Картинка: `GET {image_url}` **с заголовком Authorization**
  (загружайте как blob → `URL.createObjectURL`).
* Правка: `PATCH /posts/{id} {text?, platform?, proposed_at?, target_chat_id?}` — правка согласованного поста возвращает его в draft.
* Картинка из шаблона: `POST /posts/{id}/image {title, subtitle, template, background}`; медиатека: `GET/POST /media`.
* Проверка: `POST /posts/{id}/check` → `{ok, errors[], warnings[]}` (errors блокируют отправку).
* Согласование: `submit {proposed_at?}` → `approve {scheduled_at?, publish_now?}` | `request-changes {comment, auto_revise}` |
  `reject {comment}`; `schedule {at}`. Другая площадка: `POST /posts/{id}/adapt?platform=vk`.
* Показатели опубликованного: `GET /posts/{id}/analytics`.
* `content_approved` — согласован ли текущий вариант; `checks` — результат проверки; `last_error` — причина ошибки.

**AI-материалы.** `POST /content/strategy|content-plan|video-script|topics` → `{result, ai}`.

**Аналитика.** `GET /analytics` (по каналам, `format_effectiveness`), `GET /analytics/competitors`,
`GET /reports?days=7` → `{report, text}`.

**Регулярные задачи (owner).** `GET/POST /tasks`, `PATCH/DELETE /tasks/{id}`, `POST /tasks/{id}/run`.
Расписание: `{"every_minutes": 60}` или `{"daily_at": "09:00", "weekdays": [0], "tz": "Europe/Moscow"}`.

**Комментарии.** `GET /inbox?status=new`, `POST /inbox/{id}/draft` (AI-черновик), `PATCH /inbox/{id} {reply_text}`,
`POST /inbox/{id}/send` (approver), `POST /inbox/{id}/ignore`.

**Подключение ChatGPT (админ).** `GET /api/ai/connection` (статус; `is_mock`, `warning` для тестовой модели),
`POST /api/ai/connect` → ссылка входа (работает, если браузер на машине backend), `POST /api/ai/disconnect`,
`GET /api/ai/models`.

Все пути компании начинаются с `/api/companies/{company_id}`.
