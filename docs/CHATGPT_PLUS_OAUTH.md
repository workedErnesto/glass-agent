# Подключение ChatGPT Plus: проверка возможности (P0, шаг 1)

Источник: официальная документация OpenAI «Sign in with ChatGPT» (статус Preview), проверено 2026-11-13:
quickstart, token-sharing-open-source (overview, sign-in, models-and-inference, token-reference,
errors-and-recovery, preview-limitations, self-hosted-vms), request-client-id.

## Вывод

| Сценарий размещения | Допустимо? | Что нужно |
|---|---|---|
| Open-source приложение на компьютере пользователя | **Да**, документированный путь | Ничего. Динамическая регистрация клиента |
| Open-source приложение на собственной VM | **Да**, документированный путь | OAuth проходит локально, зашифрованные учётные данные переносятся на VM по SSH |
| Облачный SaaS, обслуживающий чужие компании | **Нет без согласования** — блокирующее условие | Заявка через interest form OpenAI |

Цитаты из документации:
- «These docs explain ChatGPT plan usage for open-source and locally hosted apps. If you're interested in
  offering it in a paid or remotely hosted app, complete the interest form.»
- «ChatGPT plan usage is available to all open-source partners and selected private clients.»

**Решение для MVP:** Glass Agent разворачивается как open-source приложение на компьютере администратора
или на его собственной VM. Один аккаунт ChatGPT Plus администратора обслуживает компании этого администратора.
Коммерческий SaaS-режим помечен как незавершённый, пока OpenAI не одобрит заявку.

**Как получить доступ для облака:** заполнить форму https://openai.com/form/sign-in-with-chatgpt-interest/.
Указать продукт (AI-маркетолог для нескольких компаний), модель размещения (облако) и ожидаемый объём.
До одобрения облачный backend должен возвращать `AI_CONNECTION_UNAVAILABLE`, а не переключаться на платный API.

## Что реализовано (`app/ai/`)

| Требование промпта | Реализация |
|---|---|
| Начало авторизации | `oauth.build_authorization_url`: PKCE S256, state, nonce, `ext_agent_host_id`, `agent_name_hint` |
| OAuth callback | `oauth.LoopbackCallbackServer` на `http://127.0.0.1:1455/auth/callback`, `parse_callback` (проверка state, отказ, незавершённая регистрация) |
| Проверка разрешений | scope `chatgpt.tokens.use.direct` проверяется до каждого запроса, иначе `AI_CONNECTION_UNAVAILABLE` |
| Защищённое хранение | `token_storage.EncryptedTokenStorage`: Fernet, файл 0600, атомарная запись; токены не попадают в API и логи |
| Обновление токенов | `token_refresh.TokenManager`: за 5 минут до истечения; одновременные запросы ждут одного обновления (блокировка в процессе и файловая между процессами); refresh-токен ротируется; при `invalid_grant` и подобных — `reauth_required` |
| Запросы к модели | `connectors/chatgpt_plus.py`: `POST /v1/responses`, `store:false`, `stream:true`, история целиком в `input`, без `temperature` и `max_output_tokens` |
| Потоковые ответы | разбор SSE: `response.output_text.delta`, `response.completed`, `response.failed`, `response.incomplete` |
| Ошибки и лимиты | `subscription_sharing_*` → `AIUsageLimitExceeded` (с паузой запросов), `AIConnectionUnavailable`, `AITemporarilyUnavailable`; при 401 — одно обновление токена и повтор |
| Отключение аккаунта | отзыв refresh-токена через `revocation_endpoint` из discovery и удаление токенов |
| Без платного API | `OPENAI_API_KEY` нигде не читается, это проверяется тестом |

## Ограничения подписки (Preview)

- Plus: 5-часовое окно лимита, общее для всех приложений пользователя. Сколько осталось, видно на https://chatgpt.com/settings/usage.
- Недоступны генерация изображений, file search, code interpreter, hosted MCP, аудио и видео на входе, Files API. **Изображения
  для постов делаются локально (Pillow-шаблоны) — §11 промпта.**
- Эмбеддинги в документации не упоминаются, поэтому RAG работает на локальной модели `multilingual-e5-small`.
- Нативные function/custom tools поддерживаются, если они сгруппированы в namespaces или переданы как
  `additional_tools` (preview-limitations). Агент пока выбирает инструменты через JSON-протокол в тексте ответа —
  это работает с любой моделью; переход на нативные инструменты — возможное улучшение.
- Ошибки 503 (`subscription_sharing_usage_unavailable`, `subscription_sharing_user_unavailable`) повторяются
  с ограниченной паузой (2 повтора, 1 и 3 с), учётные данные при этом не удаляются.
- `earliest_refresh_at` из ответа токена учитывается: раньше этого времени токен не обновляется, пока действует.
- Для учётных данных, перенесённых на VM, OpenAI пока не поддерживает отдельный учёт расхода и отзыв доступа по хосту.

## Как проверить на реальном аккаунте

```bash
.venv/bin/python -m scripts.chatgpt_login          # браузер → вход в ChatGPT → согласие
.venv/bin/python -m scripts.chatgpt_login --status
.venv/bin/python -m scripts.chatgpt_login --test   # список моделей + настоящий ответ модели
.venv/bin/python -m scripts.chatgpt_login --logout
```

Автотесты (`tests/test_chatgpt_*.py`) используют **замоканный** сервер OpenAI. Они не подтверждают реальную
интеграцию: она считается проверенной только после успешного `--test` на реальном аккаунте Plus.
