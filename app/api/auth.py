"""Авторизация Telegram Mini App: проверка initData (HMAC-SHA256 по токену бота).

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
Клиент передаёт заголовок `Authorization: tma <initData>`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, *, max_age_s: int = 86400,
                       now: float | None = None) -> dict[str, Any]:
    if not init_data:
        raise InitDataError("initData отсутствует")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("В initData нет hash")
    # все поля, кроме hash (включая signature), входят в строку проверки
    data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("Подпись initData неверна")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as e:
        raise InitDataError("Некорректный auth_date") from e
    if max_age_s and (now or time.time()) - auth_date > max_age_s:
        raise InitDataError("initData устарели")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as e:
        raise InitDataError("Некорректный user") from e
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("В initData нет пользователя")
    return {"user": user, "auth_date": auth_date, "query_id": pairs.get("query_id")}


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Подписывает initData (для тестов и локальной проверки)."""
    from urllib.parse import urlencode

    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})
