"""Получение ключа доступа VK по ссылке через своё приложение VK (официальный OAuth VK).

1. Создайте приложение VK (тип Standalone / «Сайт») и впишите его ID в .env: VK_APP_ID=...
2. Запустите:  python -m scripts.vk_token
3. Откроется страница VK — войдите и разрешите доступ. VK перенаправит на
   https://oauth.vk.com/blank.html#access_token=...
4. Скопируйте адрес из адресной строки и вставьте в терминал (ввод скрыт).
   Скрипт проверит ключ на сообществе и сохранит его в .env (VK_ACCESS_TOKEN).

Можно вставить и готовый «Сервисный ключ доступа» из настроек приложения.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.core.config import get_settings
from app.parsers.vk_parser import VKError, VKParser

AUTHORIZE_URL = "https://oauth.vk.com/authorize"
REDIRECT_URI = "https://oauth.vk.com/blank.html"


def build_url(app_id: int, scope: str, api_version: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": app_id, "display": "page", "redirect_uri": REDIRECT_URI,
        "scope": scope, "response_type": "token", "v": api_version,
    })


def extract_token(value: str) -> tuple[str, dict[str, str]]:
    """Достаёт access_token из адреса blank.html#access_token=... или принимает голый ключ."""
    value = value.strip()
    if "access_token=" in value or "error=" in value:
        frag = urlsplit(value).fragment or urlsplit(value).query or value.split("#", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(frag).items()}
        if "error" in params:
            raise ValueError(f"VK вернул ошибку: {params.get('error')} {params.get('error_description', '')}")
        return params["access_token"], params
    if re.fullmatch(r"[A-Za-z0-9._\-]{40,}", value):
        return value, {}
    raise ValueError("Не похоже ни на адрес с access_token, ни на ключ доступа")


def save_to_env(token: str, env_path: Path = Path(".env")) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for i, line in enumerate(lines):
        if line.startswith("VK_ACCESS_TOKEN="):
            lines[i] = f"VK_ACCESS_TOKEN={token}"
            break
    else:
        lines.append(f"VK_ACCESS_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


async def check(token: str, group: str) -> str:
    s = get_settings()
    parser = VKParser(token, api_version=s.vk_api_version, api_base=s.vk_api_base)
    async with httpx.AsyncClient(timeout=20) as client:
        g = await parser.get_group(client, group)
        wall = await parser.call(client, "wall.get", owner_id=-int(g["id"]), count=1)
    return f"«{g.get('name')}» — подписчиков {g.get('members_count')}, записей на стене {wall.get('count')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="salavatsteklo", help="сообщество для проверки ключа")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    if s.vk_app_id:
        url = build_url(s.vk_app_id, s.vk_oauth_scope, s.vk_api_version)
        print("Ссылка для получения ключа (откройте, войдите в VK и разрешите доступ):\n\n" + url + "\n")
        if not a.no_browser:
            webbrowser.open(url)
    else:
        print("VK_APP_ID в .env не задан — можно вставить готовый сервисный ключ приложения.\n")
    raw = getpass.getpass("Вставьте адрес из браузера (oauth.vk.com/blank.html#access_token=…) или ключ: ")
    try:
        token, params = extract_token(raw)
    except ValueError as e:
        print("Ошибка:", e)
        return 2
    if params.get("expires_in") not in (None, "0"):
        print(f"Внимание: ключ временный (expires_in={params['expires_in']} с). Для бессрочного нужен scope=offline.")
    try:
        info = asyncio.run(check(token, a.group))
    except VKError as e:
        print(f"Ключ не прошёл проверку: {e}")
        return 3
    save_to_env(token)
    print(f"Ключ работает: {info}\nСохранён в .env (VK_ACCESS_TOKEN). Теперь:\n"
          f"  .venv/bin/python -m scripts.parse_sources --company salavatsteklo vk https://vk.ru/{a.group}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
