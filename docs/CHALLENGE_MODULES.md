# Challenge Modules Inventory & Status

## Обзор
Документ фиксирует статус и классификацию модулей в директории `challenge/` и их взаимосвязь с новым сигнальным стеком `usstocks/`.

---

## Статус модулей

### 1. Активные в signal-only профиле (`us_stocks_challenge`)
- `challenge/manual/symbols.json` — карта маппинга тикеров в `symbolId` UTEX.
- `challenge/manual/alerter.py` — legacy CLI алертер (переиспользует `usstocks/data/utex_provider.py`).

### 2. Заменённые компоненты (Deprecated for US Stocks)
- `challenge/risk.py` — устаревшая логика лимитов, полностью заменена на `usstocks/risk_engine.py` и `shared/risk_protocol.py`.
- `challenge/strategy.py` — старые эвристики, заменены на `usstocks/strategy/vwap_pullback.py`.
- `challenge/manual/outcomes.py` — заменено на `usstocks/journal.py` с версионированием схемы SQLite и таблицей `us_trade_outcomes`.

### 3. Автоторговые модули (Заблокированы в signal-only)
- `challenge/runner.py` — браузерная автоматизация через Playwright (заблокирована interlock-проверкой `assert_auto_trading_allowed`).
- `challenge/stealth/` — hotkey-исполнение и humanized mouse tracks (заблокированы interlock-проверкой).
- `challenge/browser.py`, `challenge/windows.py`, `challenge/login.py` — браузерные интерфейсы, не используемые в `signal-only` режиме.

---

## Защита от случайного запуска
Все исполняющие точки входа защищены вызовом `usstocks.guards.assert_auto_trading_allowed()`, завершающим процесс с кодом `2` при запуске под профилями `us_stocks_challenge` или `replay`.
