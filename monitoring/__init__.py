"""Monitoring package (ТЗ Часть 6): metrics, alerts, health checks.

Phase 4, Step 12:
    - ``monitoring.health``   — component checks for the enriched /api/health
                                endpoint (ТЗ 6.3);
    - ``monitoring.metrics``  — execution metrics collector (ТЗ 6.1);
    - ``monitoring.alerts``   — alert manager with rules + cooldowns (ТЗ 6.2);
    - ``monitoring.disk``     — free-disk-space check (ТЗ 6.18 / P2-31).

Everything here is observability-only: no trading behaviour is changed.
"""
