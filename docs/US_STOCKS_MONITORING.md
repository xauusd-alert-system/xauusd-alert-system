# US Stocks Scanner Monitoring & Metrics

## 1. Health Check Endpoints (`usstocks/health_server.py`)

Сервер запускается на FastAPI (порт 8000 по умолчанию):

- **`GET /api/health`**:
  ```json
  {
    "status": "healthy",
    "uptime_seconds": 3600.5,
    "last_scan_timestamp": 1724760000.0,
    "total_scans": 120,
    "signals_generated": 2,
    "signals_enabled": true,
    "day_stopped": false
  }
  ```
- **`GET /api/status`**: детальная сводка состояния сессии и активных сигналов.
- **`GET /api/metrics`**: JSON-дамп метрик задержки и счётчиков сканирования.
- **`GET /metrics`**: стандартный Prometheus-экспорт (`MetricsRegistry` в `shared/metrics.py`).

---

## 2. Метрики производительности
- `scan_cycle_duration_seconds`: время выполнения полного цикла сканирования списка тикеров.
- `symbol_scan_duration_seconds`: время обработки отдельного тикера (загрузка данных + расчёт индикаторов + чеклист).
- Предупреждение логируется при длительности цикла `>10.0s`.

---

## 3. Circuit Breaker (`shared/circuit_breaker.py`)
- Защищает запросы к UTEX API.
- Порог: 5 ошибок подряд переводят состояние в `OPEN` на 60 секунд (`recovery_timeout`), предотвращая перегрузку и спам запросов при разрывах связи.
