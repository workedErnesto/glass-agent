"""Сборка AI-коннектора из настроек."""
from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings

from .auth.token_refresh import TokenManager
from .auth.token_storage import EncryptedTokenStorage, load_fernet
from .connectors.chatgpt_plus import ChatGPTPlusConnector


@lru_cache
def get_token_manager() -> TokenManager:
    s = get_settings()
    fernet = load_fernet(s.glass_secret_key, s.master_key_path)
    return TokenManager(EncryptedTokenStorage(s.chatgpt_credentials_path, fernet))


@lru_cache
def get_chatgpt_connector() -> ChatGPTPlusConnector:
    s = get_settings()
    return ChatGPTPlusConnector(get_token_manager(), model=s.chatgpt_model,
                                limit_cooldown_s=s.chatgpt_limit_cooldown_s)


def get_ai_connector():
    """Коннектор по AI_PROVIDER: chatgpt_plus (по умолчанию) или mock_dev (ТЕСТОВАЯ модель, только явно)."""
    import logging

    s = get_settings()
    if s.ai_provider == "mock_dev":
        from .connectors.dev_mock import DevMockConnector

        logging.getLogger(__name__).warning("AI_PROVIDER=mock_dev: используется ТЕСТОВАЯ модель, ответы не от ChatGPT")
        return DevMockConnector()
    if s.ai_provider != "chatgpt_plus":
        raise ValueError(f"AI_PROVIDER={s.ai_provider!r}: допустимо chatgpt_plus или mock_dev")
    return get_chatgpt_connector()
