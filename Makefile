# Частые команды. Python из .venv.
PY ?= .venv/bin/python
DEV_TG_ID ?= 1

.PHONY: install db migrate api dev-mock bot scheduler test doctor openapi

install:            ## зависимости и .env
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cp -n .env.example .env || true
db:                 ## PostgreSQL + pgvector в Docker
	docker compose up -d db
migrate:            ## миграции до последней ревизии
	$(PY) -m scripts.manage init-db
api:                ## REST API на http://127.0.0.1:8000 (документация /docs)
	$(PY) -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --reload
dev-mock:           ## API с ТЕСТОВОЙ моделью и входом без initData (для разработки фронтенда)
	AI_PROVIDER=mock_dev API_DEV_USER_ID=$(DEV_TG_ID) ADMIN_TELEGRAM_IDS=$(DEV_TG_ID) $(PY) -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --reload
bot:                ## Telegram-бот
	$(PY) -m scripts.bot
scheduler:          ## публикации по расписанию и регулярные задачи
	$(PY) -m scripts.scheduler
test:               ## тесты (нужна БД: make db)
	$(PY) -m pytest -q
doctor:             ## проверка окружения
	$(PY) -m scripts.doctor
openapi:            ## схема API для фронтенда
	$(PY) -m scripts.manage export-openapi docs/openapi.json
