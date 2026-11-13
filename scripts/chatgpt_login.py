"""Подключение аккаунта ChatGPT Plus через официальный Sign in with ChatGPT.

Запускает приём redirect на http://127.0.0.1:1455/auth/callback, открывает
браузер со страницей входа OpenAI. Вход и согласие выполняет владелец
аккаунта в браузере. Затем: обмен кода (PKCE), проверка id_token, проверка
разрешения chatgpt.tokens.use.direct, зашифрованное сохранение токенов,
список моделей и тестовый запрос.

    python -m scripts.chatgpt_login            # вход + проверка
    python -m scripts.chatgpt_login --status   # статус подключения
    python -m scripts.chatgpt_login --test     # только тестовый запрос
    python -m scripts.chatgpt_login --logout   # отзыв токена и удаление
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser

from app.ai.auth.flow import finish_login
from app.ai.auth.oauth import (
    DYNAMIC_CLIENT_ID, REQUIRED_SCOPE, AuthAttempt, LoopbackCallbackServer, OAuthError,
    build_authorization_url, load_or_create_host_id, parse_callback,
)
from app.ai.connectors.base import AIError, AIMessage
from app.ai.factory import get_chatgpt_connector, get_token_manager
from app.core.config import get_settings


async def login() -> int:
    s = get_settings()
    tm = get_token_manager()
    host_id = load_or_create_host_id(s.chatgpt_host_id_path)
    prev = tm.load()
    client_id = prev.client_id if prev else DYNAMIC_CLIENT_ID
    async with LoopbackCallbackServer(s.chatgpt_callback_port) as server:
        attempt = AuthAttempt(client_id=client_id, redirect_uri=server.redirect_uri)
        url = build_authorization_url(attempt, host_id=host_id, agent_name=s.chatgpt_agent_name,
                                      login_hint=prev.email if prev else None,
                                      prompt="consent" if prev else None)
        print("Откройте ссылку и войдите в ChatGPT (если браузер не открылся сам):\n")
        print(url, "\n")
        webbrowser.open(url)
        print(f"Жду ответ на {server.redirect_uri} (до 5 минут)…")
        query = await server.wait(300)
    try:
        cb = parse_callback(query, attempt)
    except OAuthError as e:
        print(f"Авторизация не выполнена: {e}")
        return 2
    creds = await finish_login(cb, attempt, host_id, tm)
    print(f"\nВход выполнен: {creds.email or '(email не передан)'}")
    print(f"Разрешения: {' '.join(creds.scopes)}")
    if REQUIRED_SCOPE not in creds.scopes:
        print(f"\nAI_CONNECTION_UNAVAILABLE: разрешение {REQUIRED_SCOPE} не выдано — использовать подписку нельзя.")
        return 3
    return await test()


async def test() -> int:
    conn = get_chatgpt_connector()
    try:
        models = await conn.list_models()
        print("Доступные модели:", ", ".join(m["slug"] for m in models) or "—")
        print("\nТестовый запрос к модели (подписка ChatGPT Plus, без API-ключа)…")
        res = await conn.complete([AIMessage("user", "Ответь ровно одной фразой: «Glass Agent подключён»")])
        print(f"Ответ [{res.model}]: {res.text}")
        print("usage:", res.usage)
        return 0
    except AIError as e:
        print(f"{e.code}: {e} (reason={e.reason})")
        return 4


async def status() -> int:
    import json
    print(json.dumps(await get_chatgpt_connector().status(), ensure_ascii=False, indent=2, default=str))
    return 0


async def logout() -> int:
    await get_chatgpt_connector().disconnect()
    print("Аккаунт ChatGPT отключён, токены удалены.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--test", action="store_true")
    g.add_argument("--logout", action="store_true")
    a = ap.parse_args()
    fn = status if a.status else test if a.test else logout if a.logout else login
    return asyncio.run(fn())


if __name__ == "__main__":
    sys.exit(main())
