# Развёртывание 24/7 на своей VM

```bash
git clone … && cd glass
cp .env.example .env            # TG_BOT_TOKEN, ADMIN_TELEGRAM_IDS, GLASS_SECRET_KEY, MAX/VK при наличии
docker compose up -d db
docker compose --profile app up -d --build      # api (127.0.0.1:8000), bot, scheduler
docker compose logs -f bot scheduler
```

* Первая сборка скачивает зависимости (~1,5 ГБ, torch для CPU); модель эмбеддингов скачивается при первом
  запросе в `data/hf-cache`.
* Миграции применяются автоматически при старте процессов.
* API слушает только 127.0.0.1 — для Mini App поставьте перед ним nginx/Caddy с TLS и `CORS_ORIGINS`.
* `SCHEDULER_IN_API=false` (по умолчанию) — планировщик отдельным сервисом `scheduler`.

## ChatGPT Plus на VM (по документации OpenAI self-hosted-vms)

Redirect входа идёт на `127.0.0.1` машины с браузером, поэтому:
1. На своём компьютере (тот же код и тот же `GLASS_SECRET_KEY`): `python -m scripts.chatgpt_login`.
2. Перенести зашифрованный файл по SSH:
   `scp data/secrets/chatgpt_credentials.enc vm:glass/data/secrets/`
   (если ключ не задан в `.env` — ещё `data/secrets/master.key`). `agent_host_id` не переносить: у VM свой.
3. На VM: `docker compose exec api python -m scripts.chatgpt_login --status`.
4. Дальше токен обновляет VM; на компьютере файл удалить (refresh-токен одноразовый — два места его «сожгут»).

Ограничение OpenAI: для перенесённых сессий пока нет отдельного учёта расхода и отзыва по хосту.

## Резервные копии

```bash
docker exec glass-db pg_dump -U glass -d glass -Fc > backup_$(date +%F).dump
tar czf data_$(date +%F).tgz data/       # секреты, загруженные файлы, изображения
```
