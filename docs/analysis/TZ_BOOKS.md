# TZ_BOOKS.md — Задачи по интеграции знаний из книг в xauusd-alert-system

**Создано:** 2026-08-28
**Источники:**
- `docs/analysis/analysis_neuronetworksbook_20260828.md` — Neural Networks for Algorithmic Trading with MQL5 (690 стр., MetaQuotes)
- `docs/analysis/analysis_mql5book_20260828.md` — MQL5 Programming for Traders (2047 стр., MetaQuotes)

**Легенда приоритетов:** P0 — критично, немедленно (0–3 дня) · P1 — важно (1–2 недели) · P2 — желательно (1 месяц) · P3 — отложенно (1–3 месяца)

---

## P0 — Немедленные действия

- [x] **[T-01] [Architecture] Подключить NeuroBook.** Клонировать/подключить `\MQL5\Shared Projects\NeuroBook` в проект, зафиксировать версию в репозитории. (NN: стр. 5)
- [x] **[T-02] [Data Processing] Генератор выборок для XAUUSD.** Перенести паттерн `create_initial_data.mq5`: XAUUSD M5, фичи RSI+MACD+геометрия свечи, нормализация с **сохранением параметров** для live-режима, деление 60/20/20 по времени. (NN: стр. 222–229)
- [x] **[T-03] [Backtesting] Регламент валидации.** Train/valid/test 60/20/20; форвард ≥ 1 год на реальных тиках; пороги приёмки модели: форвард PF > 1.2, win-rate > 55%, порог сигнала ≥ 0.6. (NN: гл. 7; MQL5: 6.5.1, 6.5.6)
- [x] **[T-04] [ML/Model] Базовая FC-модель как контрольная точка.** 60 нейронов, Swish, Adam, Linear-выход — прогон на XAUUSD-фичах по протоколу книги. (NN: стр. 245–246, 254)
- [x] **[T-05] [Execution] Транзакционный контур исполнения.** OrderCheck → OrderSend → OnTradeTransaction с обработкой retcode; асинхронная отправка для скорости. (MQL5: 6.4.9–6.4.13, 6.4.35)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*
- [x] **[T-06] [Risk] Риск-калькулятор объёма.** Расчёт лота от риска % депозита через SYMBOL_TRADE_TICK_VALUE/SIZE, округление к VOLUME_STEP; предпроверка маржи OrderCalcMargin. (MQL5: 6.1, 6.4.7–6.4.8)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*
- [x] **[T-07] [Alerts] Каналы доставки.** SendNotification (push) + SendMail; WebRequest-хук в Telegram/бэкенд; whitelist URL в настройках терминала. (MQL5: 7.5)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*
- [x] **[T-08] [Backtesting] Тестер-контур.** Валидация на реальных тиках; кастомный критерий OnTester (PF × sqrt(trades) − штраф за DD); сбор equity через фреймы. (MQL5: 6.5.1, 6.5.6–6.5.11)

## P1 — Краткосрочные (1–2 недели)

- [x] **[T-09] [ML/Model] MH Attention-модель.** 8 голов, window_out=8, Adam; сравнение с FC/LSTM на XAUUSD (MSE-кривые + форвард). (NN: гл. 5.2, стр. 512–513)
- [x] **[T-10] [Risk] Порог сигнала + day-of-week фильтр.** TradeLevel-аналог (0.6 по умолчанию); фильтр дней недели по собственной статистике XAUUSD. (NN: стр. 688–689)
- [x] **[T-11] [ML/Model] Gradient check в CI.** Численная проверка градиентов (гл. 3.10) как автотест библиотеки слоёв. (NN: стр. 230–239)
- [x] **[T-12] [Execution] Money management и сопровождение.** SL/TP с учётом спреда XAUUSD, трейлинг, частичное закрытие. (NN: стр. 687; MQL5: 6.4.15–6.4.17)
- [x] **[T-13] [Architecture] Событийная модель сбора данных.** OnTick/OnTimer/spy-индикатор вместо опросов; стакан OnBookEvent при необходимости. (MQL5: 6.4.1, 6.4.38)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*
- [x] **[T-14] [Data Processing] Фичагенерация через хэндлы.** CopyBuffer с проверкой готовности индикаторов; SQLite-журнал сигналов (идемпотентность, дедупликация). (MQL5: 5.5, 7.6)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*
- [x] **[T-15] [Risk] Новостной фильтр (live).** Экономический календарь: блок сигналов ±N мин вокруг USD-событий высокой важности; для бэктеста — своя таблица новостей в SQLite. (MQL5: 7.3)
- [x] **[T-16] [Integration] Python-мост.** Пакет MetaTrader5: данные/инференс в Python, исполнение в MQL5; обмен через SQLite/файлы; автоторговлю Python отключить (ошибка 10027). (MQL5: 7.9)
- [x] **[T-17] [ML/Model] Мониторинг Train/Val-расхождения.** Пурга-разрез валидации по времени; алерт на расхождение кривых. (NN: стр. 255–256)

## P2 — Желательные (1 месяц)

- [x] **[T-18] [Architecture] OpenCL-ускорение.** Инференс NN в EA через OpenCL-кернелы книги; замер ускорения против CPU. (NN: гл. 3.7+; MQL5: 7.10)
- [x] **[T-19] [Features] Расширение фичесета.** ATR, волатильность сессий, объёмы — по схеме нормализации T-02. (NN: стр. 87–99)
- [x] **[T-20] [Architecture] Конфигурация моделей.** Архитектуры как CLayerDescription-конфиги, не хардкод; сериализация моделей в файлы. (NN: гл. 3.4)
- [x] **[T-21] [Architecture] MQL5-проект.** Перевод репозитория на систему проектов MQL5 с разделяемой библиотекой. (MQL5: 7.8)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*
- [x] **[T-22] [Risk] Аудит сигналов в SQLite.** Полная трасса: признаки → решение модели → алерт → исполнение → результат. (MQL5: 7.6)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*

## P3 — Долгосрочные (1–3 месяца)

- [x] **[T-23] [Backtesting] Walk-forward-конвейер.** Скользящие окна обучения/форворда, автоматическое переобучение, мониторинг дрейфа распределений. (NN: гл. 7)
- [ ] **[T-24] [ML/Model] GPT-блоки.** Авторегрессионный прогноз пути цены — после стабилизации walk-forward и при наличии GPU. (NN: гл. 5.3)
- [x] **[T-25] [ML/Model] Ансамбль архитектур.** FC+LSTM+MHAttention голосование с порогом 0.6; оценка устойчивости сигнала. (NN: гл. 4–6)
- [x] **[T-26] [Architecture] Кастомные символы.** Синтетические индикаторы риска (нормированная волатильность золота), нестандартные таймфреймы. (MQL5: 7.2)
       *(код написан; контрольная сборка в MetaEditor pending — см. docs/BOOKS_INTEGRATION_REPORT.md)*

---

## Связь с анализами

| Задача | Книга-источник | Раздел анализа |
|--------|----------------|----------------|
| T-01…T-04, T-09, T-11, T-17, T-19, T-20, T-23…T-25 | Neural Networks for Algorithmic Trading with MQL5 | «Применение в xauusd-alert-system» (раздел 8) |
| T-05…T-08, T-12…T-16, T-18, T-21, T-22, T-26 | MQL5 Programming for Traders | «Применение в xauusd-alert-system» (раздел 8) |
