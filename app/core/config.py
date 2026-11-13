"""Настройки приложения (читаются из окружения / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    data_dir: Path = Path("data")

    # Telegram / Telethon
    tg_api_id: int | None = None
    tg_api_hash: SecretStr | None = None
    tg_session_path: Path = Path("data/secrets/telethon.session")
    # Telegram Bot API
    tg_bot_token: SecretStr | None = None

    # Telegram: сбор открытого канала через веб-страницу t.me/s/<имя> (без ключей)
    tg_web_channel: str | None = None  # имя канала или ссылка: salstek, @salstek, https://t.me/salstek
    tg_web_poll_interval: int = 60  # секунд между проверками
    tg_web_initial_pages: int = 1  # сколько страниц (по ~20 постов) взять при первом запуске
    tg_web_company: str = "salavatsteklo"  # slug компании, в базу знаний которой сохраняются посты
    tg_web_log_path: Path = Path("logs/telegram_web.log")

    # VK API
    vk_access_token: SecretStr | None = None
    vk_api_version: str = "5.199"
    vk_api_base: str = "https://api.vk.ru/method"
    vk_app_id: int | None = None  # ID приложения VK для получения ключа по ссылке (scripts.vk_token)
    vk_oauth_scope: str = "offline"  # минимально нужные права: только бессрочный доступ к открытым данным
    # ключ администратора сообщества для публикации и ответов (права wall,photos,offline); лучше — секрет компании vk_token
    vk_publish_token: SecretStr | None = None

    # MAX (dev.max.ru): токен бота — business.max.ru → Чат-боты → Настройки
    max_bot_token: SecretStr | None = None
    max_api_base: str = "https://platform-api2.max.ru"

    # Модель: chatgpt_plus (по умолчанию) | mock_dev (ТЕСТОВАЯ модель для разработки фронтенда, ответы помечены)
    ai_provider: str = "chatgpt_plus"

    # ChatGPT Plus (официальный Sign in with ChatGPT, без OPENAI_API_KEY)
    chatgpt_model: str | None = None  # пусто = первая доступная модель аккаунта из /v1/models
    chatgpt_credentials_path: Path = Path("data/secrets/chatgpt_credentials.enc")
    chatgpt_host_id_path: Path = Path("data/secrets/agent_host_id")
    chatgpt_callback_port: int = 1455
    chatgpt_agent_name: str = "Glass Agent"
    chatgpt_limit_cooldown_s: int = 900  # пауза запросов после исчерпания лимита Plus

    # Шифрование секретов (Fernet). Пусто = ключ генерируется в data/secrets/master.key
    glass_secret_key: SecretStr | None = None
    master_key_path: Path = Path("data/secrets/master.key")

    # PostgreSQL + pgvector
    database_url: str = "postgresql+asyncpg://glass:glass@127.0.0.1:5433/glass"

    # RAG
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384
    reranker_model: str | None = None  # например cross-encoder/mmarco-mMiniLMv2-L12-H384-v1; пусто = без reranking

    # Агент: ограничения на один запрос (расход лимитов ChatGPT Plus)
    agent_max_steps: int = 6
    agent_max_llm_calls: int = 8
    agent_max_seconds: int = 240
    ai_daily_call_limit: int = 300  # вызовов модели на компанию в сутки (0 = без лимита)

    # Изображения: локальная генеративная модель (diffusers), пусто = только шаблоны Pillow
    local_image_model: str | None = None

    # Публикация и планировщик
    scheduler_interval: int = 30
    scheduler_in_api: bool = False  # запускать планировщик внутри процесса API

    # REST API / Telegram Mini App
    initdata_max_age_s: int = 86400
    admin_telegram_ids: str = ""  # через запятую: кто может подключать/отключать ChatGPT
    api_dev_user_id: int | None = None  # ТОЛЬКО для локальной разработки: вход без initData под этим telegram_id
    upload_max_mb: int = 20
    cors_origins: str = ""  # через запятую: адрес Mini App, например https://app.example.com
    mini_app_url: str | None = None  # кнопка «Открыть приложение» в боте

    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_telegram_ids.replace(" ", "").split(",") if x}


@lru_cache
def get_settings() -> Settings:
    return Settings()
