# ============================================
# 0. Сначала перейди в каталог проекта (важно!)
# ============================================
Set-Location -LiteralPath 'C:\Users\botbo\Desktop\xauusd-alert-system'

# Активируй venv (в текущем окне)
.\venv\Scripts\Activate.ps1

# ============================================
# ⚠ ТОКЕН UI (LEDGER_OWNER_TOKEN)
# ============================================
# Node UI (порт 3000) сам читает корневой .env и АВТОМАТИЧЕСКИ подставляет
# LEDGER_OWNER_TOKEN в страницу (sessionStorage + поле ввода) — вставлять его
# вручную НЕ нужно, если в .env есть строка:
#
#   LEDGER_OWNER_TOKEN=xauusd-owner-...
#
# Проверка:  python -c "from config.loader import get_env; print('OK' if get_env('LEDGER_OWNER_TOKEN') else 'MISSING')"
# В консоли Node-прокси при старте будет: "Owner token auto-inject: ENABLED".
#
# Если авто-подстановка не сработала (нет .env / другой рабочий каталог) —
# запасной вариант: задай переменную окружения перед запуском окна 2:
#
#   $env:LEDGER_OWNER_TOKEN = ((Get-Content 'C:\Users\botbo\Desktop\xauusd-alert-system\.env' | Where-Object { $_ -match '^LEDGER_OWNER_TOKEN=' }) -replace '^LEDGER_OWNER_TOKEN=','').Trim()

# ============================================
# 1. Скачай историю за 2 года (нужен запущенный MT5-терминал!)
#    ВАЖНО: это MT5-бэкфилл (scripts.backfill_data), НЕ scripts.seed_db —
#    seed_db тянет данные из Twelve Data и требует TWELVEDATA_API_KEY,
#    которого в .env нет. backfill_data берёт историю прямо из терминала.
# ============================================

python -m scripts.backfill_data --asset XAUUSD --timeframe M15 --start 2024-01-01 --end 2026-08-18
python -m scripts.backfill_data --asset XAGUSD --timeframe M15 --start 2024-01-01 --end 2026-08-18
python -m scripts.backfill_data --asset BTCUSD --timeframe M5 --start 2024-01-01 --end 2026-08-18
python -m scripts.backfill_data --asset EURUSD --timeframe H1 --start 2024-01-01 --end 2026-08-18
python -m scripts.backfill_data --asset GBPUSD --timeframe H1 --start 2024-01-01 --end 2026-08-18

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

# Примечание: если выведет "Retraining safety freeze is active; train_all_assets
# skipped." — это НОРМАЛЬНО (в config.yaml retraining.enabled: false, модели уже
# существуют и заморожены). Ошибкой это не является, шаг можно пропустить.

# ============================================
# 4. Проверь бэктест
# ============================================

python -m scripts.run_backtest --asset XAUUSD --timeframe M15 --db-path data/market_data_mt5.sqlite

# Примечание: если выведет "LOCKED HOLD-OUT VIOLATION: ... overlap the reserved
# period 2026-08-08..None" — это тоже НОРМАЛЬНО: бэктест защищён от заглядывания
# в зарезервированный живой период (с 08.08.2026 и до бесконечности). Перезапуск
# с --allow-locked «сожжёт» этот лок — не делай этого без явной необходимости.

# ============================================
# 5. Запусти всё в отдельных окнах
# ============================================

# ⚠ ОКНО ПОЛНОЙ МОЩИ (TRIAL, демо-счёт FxPro) — до ПЯТНИЦЫ 23:59 UTC:
# Конфиг уже разблокирован (demo_systematic, 5 активов, лимиты подняты).
# Окно действует до 21.08.2026 (пятница) 23:59 UTC — до закрытия торговой недели.
# Фоновый таймер (PID см. ниже) по истечении сам:
#   1) остановит трейдера, 2) сгенерирует отчёт docs/TRIAL_WINDOW_REPORT_*.md,
#   3) вернёт config/config.yaml из снапшота config/backup/config.yaml.pre_trial_48h_*.yaml
# Проверка таймера:  Get-Content logs\trial_window_heartbeat.txt   (строка ends_at)
# Статус окна:       python -m scripts.trial_window report | revert | watch
# ВАЖНО: трейдера и бэкенд запускай ТОЛЬКО ПОСЛЕ того, как конфиг разблокирован
# (они читают конфиг при старте). Если они уже запущены со старым конфигом —
# перезапусти их.
#
# 📰 ФИД НОВОСТЕЙ (News Guard / сентимент):
# Один фоновый Chromium живёт в окне 3 (scripts.news_feed_server, порт 8765) —
# страница www.forexfactory.com/calendar проходит Cloudflare, VPN/прокси НЕ нужны.
# Бэкенд и трейдер ходят к нему по HTTP, браузер больше НЕ открывается повторно.
# Проверка:  curl.exe http://127.0.0.1:8765/health
# Если фид недоступен — News Guard блокирует сделки (fail_closed).

# Окно 1: Python FastAPI backend
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system'; .\venv\Scripts\Activate.ps1; Write-Host '=== PYTHON BACKEND ===' -ForegroundColor Green; uvicorn realtime.app:app --host 127.0.0.1 --port 8000"

# Окно 2: Node.js UI proxy (токен подхватится из корневого .env автоматически)
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system\UI 3.7 flsah updated v3'; Write-Host '=== NODE UI PROXY ===' -ForegroundColor Cyan; npm install; npm run dev"

# Окно 2 (ЗАПАСНОЙ вариант, если авто-подстановка токена не работает):
# Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system\UI 3.7 flsah updated v3'; $env:LEDGER_OWNER_TOKEN = ((Get-Content 'C:\Users\botbo\Desktop\xauusd-alert-system\.env' | Where-Object { $_ -match '^LEDGER_OWNER_TOKEN=' }) -replace '^LEDGER_OWNER_TOKEN=','').Trim(); Write-Host '=== NODE UI PROXY (token from .env) ===' -ForegroundColor Cyan; npm install; npm run dev"

# Окно 3: News feed browser service (один фоновый Chromium, порт 8765)
# ВАЖНО: запускать ДО бэкенда/трейдера. Браузер откроется один раз и будет
# висеть в фоне (окно за экраном). Бэкенд и трейдер ходят к нему по HTTP.
# Если при запуске пишет "Port 8765 already in use" - сервис уже работает,
# новое окно Chromium НЕ откроется (защита от дублей), просто закройте окно.
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system'; .\venv\Scripts\Activate.ps1; Write-Host '=== NEWS FEED BROWSER SERVICE (port 8765) ===' -ForegroundColor Yellow; python -m scripts.news_feed_server --port 8765"

# Окно 4: MT5 Trader (DEMO, trial до пятницы 23:59 UTC)
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd 'C:\Users\botbo\Desktop\xauusd-alert-system'; .\venv\Scripts\Activate.ps1; Write-Host '=== MT5 TRADER (DEMO TRIAL!) ===' -ForegroundColor Red; python -m execution.mt5_trader"

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
Write-Host "  - Trader:   DEMO (FxPro demo, trial до пятницы 23:59 UTC)"
Write-Host "`nPress any key to exit this window..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")