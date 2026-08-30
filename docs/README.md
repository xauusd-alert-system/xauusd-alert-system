# Документация — индекс

Проект: `xauusd-alert-system`. Язык проекта — русский, технические термины — EN.

## Основные (актуальные)

| Документ | О чём |
|---|---|
| [README.md](README.md) | Этот индекс — инвентаризация docs/ (ТЗ 11.1) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Архитектура: пайплайн данные→фичи→модели→ансамбль→исполнение→леджер, карта пакетов, потоки, точки расширения (ТЗ 11.2) |
| [OPERATIONS.md](OPERATIONS.md) | Runbook: запуск/останов, health/метрики, алерты, бэкап, миграции, безопасность, overnight (ТЗ 11.3) |
| [MIGRATIONS.md](MIGRATIONS.md) | БД-миграции (data/migrate.py, 001–003), schema registry контрактов, версионирование config_hash/фичей/меток |
| [RECOVERY.md](RECOVERY.md) | Восстановление после сбоя (backup_db --restore, reconciliation) |
| [TODO.md](TODO.md) | План работ, статус подсистем, deferred-пункты |
| [TRADE_GROUP_SPEC.md](TRADE_GROUP_SPEC.md) | Спецификация торговых групп (TradeGroupSpec) |
| [STRATEGY_SPEC.md](STRATEGY_SPEC.md) | Спецификация стратегии |
| [TZ.md](TZ.md) | Техническое задание |
| [validation_notes.md](validation_notes.md) | Заметки по валидации |
| [benchmarks.md](benchmarks.md) | Результаты бенчмарков |

## Эксплуатация / интеграции

| Документ | О чём |
|---|---|
| [RUN_LIVE.md](RUN_LIVE.md) | Запуск live-режима |
| [POST_PULL_RUNBOOK.md](POST_PULL_RUNBOOK.md) | Действия после пулла данных |
| [Fast_Start.md](Fast_Start.md) | Быстрый старт |
| [LEDGER_BRIDGE.md](LEDGER_BRIDGE.md) | Сервис доставки леджер-событий (services/ledger_bridge) |
| [MQL5_OBSERVER_PLAN.md](MQL5_OBSERVER_PLAN.md) | План MT5 EA SignalDeskObserver |
| [PRODUCTION_TRAINING_CONTRACT.md](PRODUCTION_TRAINING_CONTRACT.md) | Контракт production-обучения моделей |
| [COMPLIANCE_DISCLOSURE.md](COMPLIANCE_DISCLOSURE.md) | Комплаенс-дисклеймер |
| [DASHBOARD_DISCLOSURE.md](DASHBOARD_DISCLOSURE.md) | Дисклеймер дашборда (honesty disclosures) |

## Исследования / аудит (исторические)

| Документ | О чём |
|---|---|
| [RESEARCH_PAIRS_SMC_ORDERBOOK.md](RESEARCH_PAIRS_SMC_ORDERBOOK.md) | Исследование парного трейдинга / SMC / orderbook |
| [CANDIDATE_WIDE_TREND_FILTERED.md](CANDIDATE_WIDE_TREND_FILTERED.md) | Кандидат-конфигурация стратегии |
| [FX_V3.md](FX_V3.md) | Версия 3 FX-стратегии |
| [GBP_FIX_STRATEGY.md](GBP_FIX_STRATEGY.md) | Стратегия GBP |
| [POSITION_QUALITY_AUDIT.md](POSITION_QUALITY_AUDIT.md) | Аудит качества позиций |
| [DEEPSEEK_V4_PRO_MAX_REAUDIT.md](DEEPSEEK_V4_PRO_MAX_REAUDIT.md) | Ре-аудит (DeepSeek) |
| [WEB_UI_HONESTY_AUDIT.md](WEB_UI_HONESTY_AUDIT.md) | Аудит честности web UI |
| [AUDIT_FIXES_2026-08-10.md](AUDIT_FIXES_2026-08-10.md) | Фиксы по аудиту 2026-08-10 |
| [AGENT_HANDOFF_2026-08-16.md](AGENT_HANDOFF_2026-08-16.md) | Handoff-заметки агента 2026-08-16 |
| [FIX_THREE_CLASS_LABEL_SPACE.md](FIX_THREE_CLASS_LABEL_SPACE.md) | Исправление 3-class label space |
| [FIX_THREE_CLASS_NO_TRADE_ABSENT.md](FIX_THREE_CLASS_NO_TRADE_ABSENT.md) | Исправление no-trade класса |