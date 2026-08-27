"""Dependency Injection container and application factory (P2-6)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config.loader import get_env, load_config
from shared.db import SQLiteConnectionPool
from usstocks.data.utex_provider import UtexClient
from usstocks.journal import UsJournal
from usstocks.models import RiskState
from usstocks.notify import TelegramNotifier
from usstocks.premarket_ranker import ScannerConfig
from usstocks.risk_engine import RiskEngine
from usstocks.scanner_loop import SignalOnlyRunner, load_symbol_ids
from usstocks.session import NySession, session_from_cfg


@dataclass
class BotContainer:
    cfg: dict
    state: RiskState
    journal: UsJournal
    provider: Any
    notifier: Any
    risk_engine: RiskEngine
    session: NySession
    scanner_config: ScannerConfig
    runner: SignalOnlyRunner


def build_container(
    cfg_path: Optional[str] = None,
    *,
    custom_provider: Optional[Any] = None,
    custom_notifier: Optional[Any] = None,
    custom_journal: Optional[UsJournal] = None,
    custom_state: Optional[RiskState] = None,
) -> BotContainer:
    """Assemble all dependencies via inversion of control."""
    import os
    if cfg_path is None:
        cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "us_stocks_challenge.yaml"
        )
    cfg = load_config(cfg_path)

    today_ny = "unknown"
    from datetime import datetime
    today_ny = datetime.now().astimezone().date().isoformat()

    state = custom_state or RiskState(session_date=today_ny)
    db_path = cfg.get("journal", {}).get("sqlite_path", "data/usstocks.sqlite")
    journal = custom_journal or UsJournal(db_path)
    journal.ensure_session(state.session_date)

    symbol_ids = load_symbol_ids()
    utex_client = UtexClient()

    if custom_provider is not None:
        provider = custom_provider
    else:
        class DefaultProvider:
            @staticmethod
            def get_bars(symbol: str, count: int):
                sid = symbol_ids.get(symbol.upper())
                if not sid:
                    raise KeyError(f"{symbol}: no symbolId mapping")
                access = utex_client.refresh_access()
                return utex_client.fetch_bars(access, sid, candles_count=count)
        provider = DefaultProvider()

    notifier = custom_notifier or TelegramNotifier()
    risk_engine = RiskEngine.from_cfg(cfg)
    session = session_from_cfg(cfg)
    scfg = ScannerConfig.from_cfg(cfg)

    base_univ = cfg.get("us_stocks", {}).get("base_universe", [])
    watchlist = [s.strip().upper() for s in base_univ[:scfg.max_watchlist_size]]

    runner = SignalOnlyRunner(
        cfg,
        provider,
        notifier,
        watchlist=watchlist,
        state=state,
        risk=risk_engine,
        journal=journal,
        symbol_ids=symbol_ids,
    )

    return BotContainer(
        cfg=cfg,
        state=state,
        journal=journal,
        provider=provider,
        notifier=notifier,
        risk_engine=risk_engine,
        session=session,
        scanner_config=scfg,
        runner=runner,
    )
