"""Однократная авторизация пользовательского аккаунта Telegram для Telethon.

Запускать самостоятельно в терминале: номер телефона, код из Telegram и
(при наличии) пароль 2FA вводит владелец аккаунта. Сессия сохраняется в
TG_SESSION_PATH (по умолчанию data/secrets/telethon.session) — храните её
как пароль, она даёт доступ к аккаунту.

    python -m scripts.telegram_login
"""
from __future__ import annotations

import asyncio
import os

from app.core.config import get_settings
from app.parsers.telegram_parser import TelethonChannelParser


async def main() -> None:
    s = get_settings()
    parser = TelethonChannelParser(s.tg_api_id, s.tg_api_hash, s.tg_session_path)
    client = parser.client()
    await client.start()  # интерактивный ввод телефона / кода / 2FA
    me = await client.get_me()
    print(f"Авторизовано: {me.first_name} (id {me.id}). Сессия: {s.tg_session_path}")
    await client.disconnect()
    try:
        os.chmod(s.tg_session_path, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
