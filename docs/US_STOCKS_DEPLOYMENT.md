# US Stocks Scanner Deployment Guide

## 1. Запуск через Docker Compose (Рекомендуемый)

```bash
# 1. Заполнить переменные окружения
cp .env.example .env
# Отредактировать TELEGRAM_BOT_TOKEN и TELEGRAM_ADMIN_CHAT_ID

# 2. Сборка и запуск
docker compose up -d --build

# 3. Просмотр логов
docker compose logs -f usstocks-bot
```

---

## 2. Локальный запуск через Makefile

```bash
# Прогон тестов с проверкой покрытия (>90%)
make test-cov

# Запуск сканера
make run-bot

# Запуск health-сервера
make run-health
```

---

## 3. Переменные окружения (.env)
- `PROFILE=us_stocks_challenge` (обязательно для запуска)
- `TELEGRAM_BOT_TOKEN=...`
- `TELEGRAM_ADMIN_CHAT_ID=...`
- `US_WATCHLIST=AMD,NVDA,TSLA,AAPL,META,MSFT`
- `HEALTH_SERVER_HOST=0.0.0.0`
- `HEALTH_SERVER_PORT=8000`
