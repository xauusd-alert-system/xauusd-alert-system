# Запуск на живой рынок / демо-счёт (MT5)
Инструкция по запуску `xauusd-alert-system` для реальной торговли (или торговли
на демо-счёте) через терминал MetaTrader 5.
> ⚠️ **Самое важное.** Бот НЕ знает разницы «реал» / «демо» — он исполняет ордера
> на том счёте, в который **залогинен ваш терминал MT5**. Чтобы торговать демо,
> просто войдите в MT5 под демо-аккаунтом. Никаких отдельных настроек «реал/демо»
> в коде нет.
>
> ⚠️ Единственный предохранитель «бумажного» режима — переменная окружения
> `DRY_RUN=1`. Запуск **без** неё = реальные ордера на привязанный к терминалу счёт.
> Это отдельно отмечено в квант-аудите (T4): «paper/shadow» — это комментарий,
> а не режим. Всегда сначала тестируйте с `DRY_RUN=1`, потом на демо.
---
## 1. Требования
- **Windows** + установленный **MetaTrader 5** (конфиг рассчитан на брокера **FxPro**,
  символы `GOLD`, `SILVER`, `BITCOIN`, `EURUSD`, `GBPUSD`).
- Терминал MT5 **запущен и залогинен** — бот подключается к уже открытому
  терминалу через `mt5.initialize()`.
- Python 3.12+ (стек зависимостей закреплён в `requirements.txt`; на Linux/macOS
  пакет `MetaTrader5` не ставится — он Windows-only).
---
## 2. Установка
```bash
# 1. Зависимости (на Windows сюда войдёт и MetaTrader5)
pip install -r requirements.txt
# 2. Конфигурация секретов
cp .env.example .env
# заполнить .env (см. ниже)
```
### Что заполнить в `.env`
| Ключ | Зачем |
|------|-------|
| `DATA_MODE=live` | Использовать реальный MT5 как источник данных |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота для алертов и управления |
| `TELEGRAM_CHAT_ID` | ID чата для уведомлений |
| `TELEGRAM_ADMIN_CHAT_ID` | ID админа — **только ему** доступны `/pause /resume /closeall` и команды со счётом. Если не задать, мутирующие команды доступны любому (не рекомендуется) |
```env
DATA_MODE=live
TELEGRAM_BOT_TOKEN=ваш_токен
TELEGRAM_CHAT_ID=ваш_чат
TELEGRAM_ADMIN_CHAT_ID=ваш_чат
```
---
# ============================================
# XAUUSD ALERT SYSTEM — FULL LAUNCH (2 YEARS)
# ============================================

# 0. Активируй venv (в текущем окне)
.\venv\Scripts\Activate.ps1

# ============================================
# 1. Скачай историю за 2 года (нужен MT5!)
# ============================================

python -m scripts.seed_db --symbol XAUUSD --timeframe M15 --start 2024-01-01 --end 2026-08-18
python -m scripts.seed_db --symbol XAGUSD --timeframe M15 --start 2024-01-01 --end 2026-08-18
python -m scripts.seed_db --symbol BTCUSD --timeframe M5 --start 2024-01-01 --end 2026-08-18
python -m scripts.seed_db --symbol EURUSD --timeframe H1 --start 2024-01-01 --end 2026-08-18
python -m scripts.seed_db --symbol GBPUSD --timeframe H1 --start 2024-01-01 --end 2026-08-18

# ============================================
# 2. Проверь данные
# ============================================

python -c "from data.storage import read_candles; df = read_candles('data/market_data_mt5.sqlite', 'M15', 'XAUUSD'); print(f'XAUUSD M15: {len(df)} candles')"
python -c "from data.storage import read_candles; df = read_candles('data/market_data_mt5.sqlite', 'M15', 'XAGUSD'); print(f'XAGUSD M15: {len(df)} candles')"
python -c "from data.storage import read_candles; df = read_candles('data/market_data_mt5.sqlite', 'M5', 'BTCUSD'); print(f'BTCUSD M5: {len(df)} candles')"
python -c "from data.storage import read_candles; df = read_candles('data/market_data_mt5.sqlite', 'H1', 'EURUSD'); print(f'EURUSD H1: {len(df)} candles')"
python -c "from data.storage import read_candles; df = read_candles('data/market_data_mt5.sqlite', 'H1', 'GBPUSD'); print(f'GBPUSD H1: {len(df)} candles')"

# ============================================
# 3. Обучи модель
# ============================================

python -m scripts.train_all_assets

# ============================================
# 4. Проверь бэктест
# ============================================

python -m scripts.run_backtest --asset XAUUSD --timeframe M15 --db-path data/market_data_mt5.sqlite

# ============================================
# 5. Запусти всё в отдельных окнах
# ============================================

# Окно 1: Python FastAPI backend
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system'; .\venv\Scripts\Activate.ps1; Write-Host '=== PYTHON BACKEND ===' -ForegroundColor Green; uvicorn realtime.app:app --host 127.0.0.1 --port 8000"

# Окно 2: Node.js UI proxy
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system\UI 3.7 flsah updated v3'; Write-Host '=== NODE UI PROXY ===' -ForegroundColor Cyan; npm install; npm run dev"

# Окно 3: MT5 Trader (DRY_RUN=1 для безопасности)
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system'; .\venv\Scripts\Activate.ps1; Write-Host '=== MT5 TRADER (LIVE!) ===' -ForegroundColor Red; python -m execution.mt5_trader"

# ============================================
# 6. Подожди и открой браузер
# ============================================

Write-Host "`n=== WAITING FOR SERVICES TO START ===" -ForegroundColor Magenta
Start-Sleep -Seconds 15

Write-Host "Opening http://localhost:3000 in browser..." -ForegroundColor Green
Start-Process "http://localhost:3000"

Write-Host "`n=== ALL DONE ===" -ForegroundColor Green
Write-Host "Windows opened:"
Write-Host "  - Backend:  http://127.0.0.1:8000"
Write-Host "  - UI:       http://localhost:3000"
Write-Host "  - Trader:   DRY_RUN mode (check window 3)"
Write-Host "`nPress any key to exit this window..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
```
---
## 5. Управление через Telegram
| Команда | Действие |
|---------|----------|
| `/status` | состояние + открытые позиции (P&L в $ и R) |
| `/positions` | все открытые позиции |
| `/why XAUUSD` | почему открыта позиция (подробно) |
| `/metrics` | микроструктурные метрики по реальному рынку |
| `/metrics today\|week\|2week\|month\|3month\|all` | подробная статистика закрытых сделок |
| `/account` | баланс/equity/маржа |
| `/pause` | перейти в dry-run (не слать ордера) |
| `/resume` | вернуть live |
| `/closeall` | экстренно закрыть все позиции |
---
## 6. Ночное переобучение (опционально)
```bash
python -m scripts.overnight
```
Бэкфилл → бэктест → ретрейн всех активов → ретрейн с реальными сделками →
deploy-guard (автооткат при регрессии) → отчёт в Telegram. Подробнее:
`deploy/overnight/README.md`.
---
## 7. Контрольный список перед реальными деньгами
- [ ] `python -m pytest` зелёный.
- [ ] Модели обучены (`output/models/*.joblib` на месте).
- [ ] Проверен `DRY_RUN=1` запуск (сигналы + алерты работают).
- [ ] Протестировано на демо-счёте несколько дней **без** `DRY_RUN`.
- [ ] Убеждён, что ордера реально исполняются: TP/SL/scale-out, объёмы, комиссии.
- [ ] `TELEGRAM_ADMIN_CHAT_ID` задан (доступ к `/closeall` только у вас).
- [ ] Перегнаны бэктесты/decision gate после последних правок сетки TP/SL и BTC.