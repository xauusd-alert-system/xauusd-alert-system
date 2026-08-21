"""
Live Multi-Asset MT5 Auto-Trader with Full Telegram Live Notifications.
Sends Entry Signals, TP1 Breakeven Alerts, and Final Close PnL Reports.
Includes Automatic Stops-Level & Digits Adjuster for BTCUSD and Altcoins.
"""
import os
import sys
import json
import time
import math
import logging
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, get_env, get_signal_grid
from config.deployment import deployment_mode, order_routing_allowed
from config.strategy_contract import strategy_identity
from data.trading_event_ledger import append_trading_event
from data.mt5_provider import initialize_mt5, shutdown_mt5, validate_symbol, fetch_closed_candles, _TIMEFRAMES
from data.trade_logger import init_trade_log_schema, log_trade_entry, log_trade_close
from data.execution_ledger import init_execution_ledger, log_execution_attempt, now_ms
from realtime.pipeline import RealtimePipeline
from alerts.telegram_bot import TelegramAlertBot
from execution.risk_manager import InstitutionalRiskManager
# Wave-0 contracts (MQL5 observer plan): SignalIntent is persisted BEFORE
# order_send; ExecutionEvent facts are enqueued into the durable outbox for
# delivery to the server ledger. Both are best-effort: a failure here must
# never block or change the trading path.
from contracts.execution_contracts import (
    ExecutionEvent,
    account_fingerprint,
    build_signal_intent,
    execution_event_id,
)
from data.intent_ledger import append_signal_intent
from data.ledger_bridge import enqueue_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("multi_asset_trader")


# Entry-context journal for the Telegram status commands (/status, /why in
# alerts/control_bot.py + alerts/status_commands.py). Keyed by MT5 position
# ticket. Overridable via env so tests/ops can redirect it. NOTE: keep in sync
# with alerts.status_commands.LIVE_POSITIONS_PATH (importing the trader from
# the alert layer would pull the whole ML stack into the bot).
LIVE_POSITIONS_PATH = os.getenv("LIVE_POSITIONS_PATH", "logs/live_positions.json")


def record_position_context(ticket: int, asset_key: str, signal: dict,
                            path: str = LIVE_POSITIONS_PATH) -> None:
    """Persist the entry context for a just-opened position, keyed by MT5
    ticket, so the status commands can answer "why did we enter" without
    touching the trading logic. Overwrites the whole open-position map
    (small N, no DB needed)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
    data[str(ticket)] = {
        "asset_key": asset_key,
        "opened_at_utc": datetime.now(timezone.utc).isoformat(),
        "bias": signal.get("bias"),
        "confidence": signal.get("confidence"),
        "regime": signal.get("regime"),
        "reasoning_summary": signal.get("reasoning_summary"),
        "entry_zone": signal.get("entry_zone"),
        "invalidation": signal.get("invalidation"),
        "targets": signal.get("targets"),
        "session": signal.get("session"),
        "leg": signal.get("leg"),
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)  # atomic write


def purge_closed_position_context(ticket: int, path: str = LIVE_POSITIONS_PATH) -> None:
    """Remove a ticket's context once the position is closed, keeping the
    file small. Called from wherever the trader detects closure."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    data.pop(str(ticket), None)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)


def configured_execution_assets(cfg: dict) -> set[str]:
    """Resolve execution allowlist; an explicit empty list means deny all."""
    execution = (cfg or {}).get("execution", {}) or {}
    if "enabled_assets" in execution:
        configured = execution.get("enabled_assets")
        if configured is None:
            raise ValueError("execution.enabled_assets must be a list, not null")
        return {str(asset) for asset in configured}
    # Legacy configs without an allowlist keep the historical enabled-asset fallback.
    return {
        key for key, value in (cfg or {}).get("assets", {}).items()
        if value.get("enabled", False)
    }


def positions_get_by_magic(symbol: str = None, magic: int = None):
    """Return positions, optionally filtered by symbol and magic, using only the
    parameters the REAL MetaTrader5 Python API accepts.

    W9/N3 (audit 2026-08-10): the production code previously called
    `mt5.positions_get(magic=...)`, but the real API only accepts `symbol`,
    `group` and `ticket` — there is no `magic` argument. The test shim added it,
    so tests/simulation passed while the live terminal raised TypeError (or
    returned positions unfiltered). The magic filter is therefore applied in
    Python via `pos.magic`, which every real MT5 position object exposes.
    """
    try:
        if symbol is not None:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()
    except Exception as e:  # pragma: no cover - defensive, mirrors prior behaviour
        logger.error(f"positions_get failed: {e}")
        return None
    if not positions:
        return positions
    if magic is None:
        return positions
    return [p for p in positions if getattr(p, "magic", None) == magic]


class MultiAssetMT5Trader:
    def __init__(self):
        self.cfg = load_config()
        self.bot = TelegramAlertBot(self.cfg)
        self.deployment_mode = deployment_mode(self.cfg)
        self.strategy_identity = strategy_identity(self.cfg)
        self.order_routing_enabled, self.order_routing_reason = order_routing_allowed(
            self.cfg, confirmed_by="startup-capability-check"
        )
        if (self.order_routing_enabled
                and self.cfg.get("deployment", {}).get("require_telegram_admin_for_execution", True)
                and not (get_env("TELEGRAM_ADMIN_CHAT_ID") or get_env("TELEGRAM_CHAT_ID"))):
            raise RuntimeError("Execution-capable deployment requires TELEGRAM_ADMIN_CHAT_ID")

        self.magic_number = 777111
        self._init_blackout()
        self._blackout_flattened = False
        self.dry_run = (os.getenv("DRY_RUN") == "1") or not self.order_routing_enabled
        self.require_demo_account = (
            self.deployment_mode.value == "demo_systematic"
            or bool(self.cfg.get("execution", {}).get("require_demo_account", False))
        )
        if self.require_demo_account and not self.dry_run:
            initialize_mt5()
            account = mt5.account_info()
            demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
            if account is None or demo_mode is None or getattr(account, "trade_mode", None) != demo_mode:
                shutdown_mt5()
                raise RuntimeError(
                    "Execution is locked: execution.require_demo_account=true and the connected "
                    "MT5 account is not a demo account. Set DRY_RUN=1 to inspect signals only."
                )
        # W8/W9: risk manager reads limits from config `execution.*` and counts
        # only our own positions (filtered by magic), not foreign/manual ones.
        self.risk_manager = InstitutionalRiskManager(self.cfg, magic=self.magic_number)
        # W2: live volume comes from config (assets.<key>.volume or backtest.volume)
        # instead of a hard-coded 0.01 that made the 50/30/20 scale-out
        # unimplementable (round(0.5*0.01,2)=0.01 closed the whole position).
        default_volume = self.cfg.get("backtest", {}).get("volume", 0.10)
        self.volume = float(self.cfg.get("execution", {}).get("volume", default_volume))
        # N11 (audit 2026-08-10): the scale-out lot validator was only invoked in
        # the backtester, so a live base lot whose 50/30/20 tranches are below the
        # MT5 volume_step went unnoticed. Fail fast at startup when the live
        # volume cannot be partial-closed (raise_on_invalid=True).
        from execution.portfolio_allocator import validate_scaleout_tranches
        is_valid, err_msg, _ = validate_scaleout_tranches(
            self.volume, [0.5, 0.3, 0.2],
            min_lot=0.01, lot_step=0.01, raise_on_invalid=True,
        )
        if not is_valid:
            raise ValueError(
                f"Live scale-out configuration invalid for volume={self.volume}: {err_msg}"
            )

        self.pipelines = {}
        assets = self.cfg.get("assets", {})
        # `enabled` permits an asset in research/data pipelines. Execution can
        # be narrower: this prevents unvalidated assets from becoming tradeable
        # simply because their data collection is enabled.
        self.execution_assets = configured_execution_assets(self.cfg)
        # Book (DOM) feed: only BTCUSD has a real book on FxPro; the poller
        # runs in its own daemon thread. The gate applied in the pipeline is
        # fail-open (no book => signal unchanged).
        from realtime.book_feed import BookFeed
        self.book_feed = BookFeed(self.cfg, persist=True)
        self.book_feed.start()
        for asset_key, a_cfg in assets.items():
            if a_cfg.get("enabled", False) and asset_key in self.execution_assets:
                try:
                    self.pipelines[asset_key] = RealtimePipeline(
                        asset_key=asset_key, cfg=self.cfg, data_mode="live",
                        book_feed=self.book_feed,
                    )
                    logger.info(f"Loaded pipeline for {asset_key}")
                except Exception as e:
                    logger.warning(f"Could not load pipeline for {asset_key}: {e}")

        # FX probes are deliberately isolated from model execution: they create
        # bounded, short-lived demo samples to calibrate spread/slippage for the
        # unapproved EURUSD/GBPUSD strategies.
        from execution.fx_execution_probe import FXProbeScheduler
        self.fx_probe_scheduler = FXProbeScheduler(self.cfg)
        if self.dry_run:
            self.fx_probe_scheduler.enabled = False

        self.be_state = {}
        self.active_trades = {}
        self.streak_losses = {}
        # W10 (audit 2026-08-10): the per-position management state (TP1/TP2/TP3
        # targets, tp1_hit/tp2_hit, be_done, trailing_active) used to live only in
        # memory, so a process restart dropped partial/breakeven management for any
        # still-open position. It is now persisted so a restart picks up where the
        # previous process left off. Runtime file under logs/ (gitignored).
        self.management_state_path = os.getenv(
            "MANAGEMENT_STATE_PATH", "logs/live_management_state.json"
        )
        self._load_management_state()

        # Early-breakeven trigger per MT5 symbol (signal_grid.breakeven_trigger_atr).
        # < 1.0 moves the stop to entry before TP1 (protects mean-reverting FX from
        # the 3x-step loss tail); 1.0 = legacy (BE only at TP1).
        self.be_trigger_by_symbol = {}
        self.trailing_atr_mult_by_symbol = {}
        # T11 (audit 2026-08-10): map each MT5 symbol to the MT5 timeframe enum of
        # its asset's trading timeframe (XAU M15, EUR/GBP H1, etc.), so the bar
        # polling below matches the timeframe each asset actually trades. Before
        # this, the loop polled TIMEFRAME_M5 for EVERY asset, re-querying an H1
        # signal every 5 minutes.
        self.symbol_timeframe = {}
        for asset_key, a_cfg in assets.items():
            sym = a_cfg.get("mt5_symbol")
            if sym:
                grid = get_signal_grid(self.cfg, a_cfg)
                self.be_trigger_by_symbol[sym] = float(grid.get("breakeven_trigger_atr", 1.0))
                # v4b trailing (None = legacy)
                self.trailing_atr_mult_by_symbol[sym] = grid.get("trailing_atr_mult")
                tf = a_cfg.get("timeframe") or self.cfg.get("market_data", {}).get("timeframe", "M5")
                self.symbol_timeframe[sym] = _TIMEFRAMES.get(tf, mt5.TIMEFRAME_M5)

        # CRIT 5: path to the executed-trades SQLite DB (the same file used by
        # scripts/retrain_with_real_trades.py). Overridable via env for tests.
        # get_env may return None; coerce to str so schema init/log calls type-check.
        self.trade_db_path = str(get_env("TRADE_LOG_DB_PATH", default=self.cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")))
        init_trade_log_schema(self.trade_db_path)
        init_execution_ledger(self.trade_db_path)
        # Track the feature/confidence snapshot we had at entry, keyed by position ticket,
        # so we can persist them via log_trade_entry(log_trade_close(...)) for ML retraining.
        self.signal_features = {}
        # Last realized PnL (money) per position ticket, captured before the position row
        # is removed from actives - used to log a meaningful close pnl.
        self.last_close_pnl = {}
        self.corr_filter_cfg = self.cfg.get("correlation_filter", {})
        self.corr_threshold = self.corr_filter_cfg.get("threshold", 0.80)
        self.corr_history_bars = self.corr_filter_cfg.get("history_bars", 500)
        self.corr_update_interval = self.corr_filter_cfg.get("update_interval_minutes", 60)
        self.corr_matrix = {}
        self.corr_last_update = 0

    # ------------------------------------------------------------------ W10
    def _save_management_state(self):
        """Persist per-position management state so a restart keeps managing
        open positions (TP targets, partial-hit flags, BE/trailing flags)."""
        if not self.active_trades:
            # Nothing open -> leave any stale file; the close-detector purges
            # entries when positions close.
            return
        directory = os.path.dirname(self.management_state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = self.management_state_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.active_trades, f, indent=2, default=str)
            os.replace(tmp_path, self.management_state_path)
        except OSError as e:
            logger.error(f"Failed to persist management state: {e}")

    def _load_management_state(self):
        """Restore persisted per-position management state on startup.

        JSON object keys are always strings, but every runtime path uses INT
        MT5 tickets (pos.ticket, check_and_move_breakeven's current_tickets,
        the close detector's set difference). Restoring string keys made a
        still-open ticket "unknown" (it was re-registered with tp1=None, losing
        its TP targets) and dropped the stale string key into the close
        detector, where history_deals_get(position="<str>") blew up and took
        the whole detection cycle (incl. Telegram close notifications) down
        with it after every restart. Coerce back to int."""
        if not os.path.exists(self.management_state_path):
            return
        try:
            with open(self.management_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                loaded = {}
                for k, v in data.items():
                    if not isinstance(v, dict) or v.get("tp1") is None:
                        continue
                    try:
                        loaded[int(k)] = v
                    except (TypeError, ValueError):
                        logger.warning(f"Skipping management-state entry with non-ticket key {k!r}")
                self.active_trades = loaded
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read management state {self.management_state_path}: {e}")

    def _normalize_stops(self, symbol: str, side: str, price: float, raw_sl: float, raw_tp: float):
        """Возвращает (sl, tp) с учётом требований MT5."""
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if not info or not tick:
            raise RuntimeError(f"symbol_info failed for {symbol}")

        digits = info.digits
        point = info.point
        stops_level = info.trade_stops_level
        freeze_level = info.trade_freeze_level
        spread = abs(tick.ask - tick.bid)
        spread_points = int(round(spread / point))
        min_dist_points = max(stops_level, freeze_level, spread_points + 30)
        min_dist = min_dist_points * point
        bid = tick.bid
        ask = tick.ask

        if side == "long":
            sl = min(raw_sl, bid - min_dist)
            tp = max(raw_tp, ask + min_dist)
        else:
            sl = max(raw_sl, ask + min_dist)
            tp = min(raw_tp, bid - min_dist)

        sl = round(round(sl / point) * point, digits)
        tp = round(round(tp / point) * point, digits)
        logger.info(
            f"Normalize {symbol} {side}: price={price}, raw_sl={raw_sl}, raw_tp={raw_tp}, "
            f"digits={digits}, point={point}, stops={stops_level}, freeze={freeze_level}, "
            f"spread_pts={spread_points}, min_dist={min_dist}, bid={bid}, ask={ask}, "
            f"final_sl={sl}, final_tp={tp}"
        )
        return sl, tp

    # ========== ДИНАМИЧЕСКИЙ КОРРЕЛЯЦИОННЫЙ ФИЛЬТР ==========

    def _fetch_close_series(self, asset_key: str, symbol: str, count: int) -> pd.Series:
        """Strategy-horizon returns indexed by UTC, never positional M5 closes."""
        try:
            asset_cfg = self.cfg.get("assets", {}).get(asset_key, {})
            timeframe = asset_cfg.get("timeframe") or self.cfg.get("market_data", {}).get("timeframe", "M5")
            df = fetch_closed_candles(symbol, timeframe, count)
            ts = pd.to_datetime(df["timestamp"], utc=True)
            return pd.Series(df["close"].astype(float).pct_change().values,
                             index=ts, name=asset_key).dropna()
        except Exception as e:
            logger.error(f"Error fetching {symbol} for correlation: {e}")
            return pd.Series(dtype=float)

    def _update_corr_matrix(self):
        """Пересчитывает матрицу корреляций на основе последних баров."""
        # Проверяем, не пора ли обновить
        now = time.time()
        if now - self.corr_last_update < self.corr_update_interval * 60:
            return
        self.corr_last_update = now

        assets = self.cfg.get("assets", {})
        symbols = {k: v["mt5_symbol"] for k, v in assets.items() if v.get("enabled")}
        closes = {}
        for asset_key, symbol in symbols.items():
            s = self._fetch_close_series(asset_key, symbol, self.corr_history_bars)
            if len(s) > 50:
                closes[asset_key] = s

        if len(closes) < 2:
            return

        table = pd.DataFrame(closes).dropna()
        if len(table) < 50:
            return

        corr = table.corr()
        self.corr_matrix = corr.to_dict()

    def _are_correlated(self, asset_a: str, asset_b: str) -> bool:
        """Проверяет, превышает ли корреляция порог (по модулю)."""
        if not self.corr_matrix:
            return False
        corr = self.corr_matrix.get(asset_a, {}).get(asset_b, 0.0)
        return abs(corr) >= self.corr_threshold

    def _has_correlated_position(self, asset_key: str, bias: str) -> bool:
        """
        Проверяет, есть ли уже открытая позиция по коррелированному активу
        с тем же направлением.
        """
        if not self.corr_filter_cfg.get("enabled", True):
            return False

        # Обновляем матрицу, если нужно
        self._update_corr_matrix()

        direction = 1 if bias == "long" else -1
        positions = positions_get_by_magic(magic=self.magic_number)
        if not positions:
            return False

        # Строим mapping symbol -> asset_key
        sym_to_asset = {v["mt5_symbol"]: k for k, v in self.cfg["assets"].items()}

        for pos in positions:
            pos_asset = sym_to_asset.get(pos.symbol)
            if not pos_asset:
                continue
            if pos_asset == asset_key:
                continue  # уже проверено ранее, но не помешает
            pos_dir = 1 if pos.type == 0 else -1
            if pos_dir == direction and self._are_correlated(asset_key, pos_asset):
                logger.info(
                    f"Correlation filter: {asset_key} {bias} blocked due to existing "
                    f"{pos_asset} position (corr >= {self.corr_threshold})."
                )
                return True
        return False

    # ========== ОСТАЛЬНАЯ ЛОГИКА (БЕЗ ИЗМЕНЕНИЙ) ==========

    def _group_position_counts(self, open_positions):
        """Map open broker positions to (groups_by_asset, singles_by_asset).

        Audit 2026-08-19 (owner request): 3-leg groups share one group_key in
        active_trades, so the risk budget counts a group as ONE slot. Tickets
        unknown to active_trades (foreign/restart edge) count as single
        positions — conservative.
        """
        groups_by_asset = {}
        singles_by_asset = {}
        for p in open_positions or []:
            try:
                ticket = int(getattr(p, "ticket", 0))
            except (TypeError, ValueError):
                continue
            trade = self.active_trades.get(ticket, {})
            asset_key = trade.get("symbol") or getattr(p, "symbol", "?")
            group_key = trade.get("group_key")
            if group_key:
                groups_by_asset.setdefault(asset_key, set()).add(group_key)
            else:
                singles_by_asset[asset_key] = singles_by_asset.get(asset_key, 0) + 1
        return groups_by_asset, singles_by_asset

    def _init_blackout(self):
        """Read execution.trading_blackout (owner request 2026-08-19): the
        trader must be OFF while the market is inactive (night/weekend) and
        while unattended. Windows are in UTC; a manual one-off halt can cover
        the current stretch (manual_halt_until_utc)."""
        bo = self.cfg.get("execution", {}).get("trading_blackout", {}) or {}
        self.blackout_enabled = bool(bo.get("enabled", False))
        db = bo.get("daily_break_utc") or []
        self.blackout_daily_break = (db[0], db[1]) if len(db) == 2 else None
        w = bo.get("weekend") or {}
        self.blackout_weekend = (
            int(w.get("start_dow", 4)), str(w.get("start_utc", "21:00")),
            int(w.get("end_dow", 0)), str(w.get("end_utc", "21:00")),
        )
        self.blackout_flatten_minutes = int(bo.get("flatten_before_minutes", 10))
        manual = bo.get("manual_halt_until_utc")
        self.blackout_manual_until = None
        if manual:
            try:
                self.blackout_manual_until = datetime.strptime(
                    manual, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                logger.warning("Ignoring bad manual_halt_until_utc=%r", manual)
                self.blackout_manual_until = None

    @staticmethod
    def _dow_utc(now_utc, dow, hm):
        h, m = map(int, hm.split(":"))
        d = now_utc - timedelta(days=(now_utc.weekday() - dow) % 7)
        cand = d.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand > now_utc:
            cand -= timedelta(days=7)
        return cand

    def _blackout_status(self, now_utc):
        """Returns (halted, reason, resume_utc). halted=True means the trader
        must not trade (manual halt or weekend window)."""
        if not self.blackout_enabled:
            return False, None, None
        if self.blackout_manual_until and now_utc < self.blackout_manual_until:
            return True, (
                f"manual halt until {self.blackout_manual_until:%Y-%m-%d %H:%M} UTC"
            ), self.blackout_manual_until
        s_dow, s_utc, e_dow, e_utc = self.blackout_weekend
        start = self._dow_utc(now_utc, s_dow, s_utc)
        end = start
        for offset in range(1, 8):
            cand = start + timedelta(days=offset)
            if cand.weekday() == e_dow:
                h, m = map(int, e_utc.split(":"))
                end = cand.replace(hour=h, minute=m, second=0, microsecond=0)
                break
        if start <= now_utc < end:
            return True, f"weekend blackout ({s_dow}:{s_utc} -> {e_dow}:{e_utc} UTC)", end
        return False, None, None

    def _in_daily_break(self, now_utc):
        """Night no-volatility window (owner request 2026-08-19): no bars form
        and no new entries while the market is inactive. The window may cross
        midnight (e.g. 22:00 -> 08:00 UTC)."""
        if not self.blackout_enabled or not self.blackout_daily_break:
            return False
        h, m = map(int, self.blackout_daily_break[0].split(":"))
        h2, m2 = map(int, self.blackout_daily_break[1].split(":"))
        start = h * 60 + m
        end = h2 * 60 + m2
        t = now_utc.hour * 60 + now_utc.minute
        if end <= start:
            return t >= start or t < end
        return start <= t < end

    def _flatten_all_positions(self, reason: str):
        """Best-effort market close of every open position of this system's
        magic (blackout halt). Existing positions are NOT touched by the daily
        break — only by the hard halt windows."""
        if not mt5.initialize():
            return
        positions = positions_get_by_magic(magic=self.magic_number)
        if not positions:
            logger.info(f"[blackout] no open positions to flatten ({reason})")
            return
        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue
            price = tick.bid if pos.type == 0 else tick.ask
            ok = self._close_partial_position(pos, price, pos.volume, label="blackout-halt")
            logger.info(f"[blackout] close #{pos.ticket} ({pos.symbol}) "
                        f"{'OK' if ok else 'REJECTED'}: {reason}")
        logger.info(f"[blackout] flatten pass done ({reason})")

    def _get_dynamic_min_confidence(self, asset_key: str) -> float:
        base = self.cfg["assets"][asset_key].get("ensemble", {}).get(
            "min_confidence_to_alert",
            self.cfg.get("ensemble", {}).get("min_confidence_to_alert", 0.60)
        )
        streak = self.streak_losses.get(asset_key, 0)
        if streak >= 2:
            extra = min(0.10, (streak - 1) * 0.03)
            return base + extra
        return base

    def check_and_move_breakeven(self):
        if not mt5.initialize():
            return
        positions = positions_get_by_magic(magic=self.magic_number)
        current_tickets = set()

        # CRITICAL: mt5.positions_get() returns None ONLY on an API error, and an
        # EMPTY tuple () when there are simply no open positions left. The old
        # `if not positions: ...; return` branch conflated the two and, worse,
        # wiped self.active_trades and returned BEFORE the close detector below
        # ever ran. So whenever the last (usually only) open position was closed
        # by the broker TP/SL, the Telegram "TRADE CLOSED" message, the
        # log_trade_close persistence and the loss-streak tracking were all
        # silently skipped. Now: on API error we keep state and retry next tick;
        # on an empty list we fall THROUGH to the close detector, which reports
        # every tracked ticket as closed.
        if positions is None:
            logger.warning(
                f"=== BE CHECK: positions_get failed (MT5 API error); keeping "
                f"management state, skipping this tick (magic={self.magic_number}) ==="
            )
            return

        logger.info(f"=== BE CHECK: found {len(positions)} positions (magic={self.magic_number}) ===")

        for pos in positions:
            ticket = pos.ticket
            symbol = pos.symbol
            current_tickets.add(ticket)
            if ticket not in self.active_trades:
                self.active_trades[ticket] = {
                    "symbol": symbol,
                    "type": "long" if pos.type == 0 else "short",
                    "entry_price": pos.price_open,
                    "original_volume": pos.volume,
                    "tp1": None, "tp2": None, "tp3": None,
                    "tp1_hit": False, "tp2_hit": False,
                }
            # CRIT 5: keep the DB logging row keyed by the same position ticket.
            if ticket not in self.signal_features:
                self.signal_features[ticket] = {
                    "symbol": symbol,
                    "type": "long" if pos.type == 0 else "short",
                    "entry_time": getattr(pos, "time", None)
                    or int((pos.price_open and 0) or 0),  # shim exposes time
                    "entry_price": pos.price_open,
                }
            if ticket in self.be_state:
                continue

            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            if not tick or not info:
                continue
            digits = info.digits
            current_price = tick.bid if pos.type == 0 else tick.ask
            trade_data = self.active_trades[ticket]
            original_volume = trade_data.get("original_volume", pos.volume)
            tp1 = trade_data.get("tp1"); tp2 = trade_data.get("tp2"); tp3 = trade_data.get("tp3")
            tp1_hit = trade_data.get("tp1_hit", False); tp2_hit = trade_data.get("tp2_hit", False)

            # EARLY BREAKEVEN (configurable): move the SL to entry once price has moved
            # be_trigger * (tp1 - entry) in our favor, BEFORE TP1. Default 1.0 keeps the
            # legacy behaviour; e.g. 0.5 for mean-reverting FX cuts the 3x-step loss tail.
            be_trigger = self.be_trigger_by_symbol.get(symbol, 1.0)
            if be_trigger < 1.0 and not tp1_hit and not trade_data.get("be_done", False) and tp1 is not None:
                step_dist = abs(tp1 - trade_data["entry_price"])
                if step_dist > 0:
                    if pos.type == 0 and current_price >= trade_data["entry_price"] + be_trigger * step_dist:
                        target_sl = round(pos.price_open + (10 ** -digits), digits)
                        min_sl = current_price - self._get_min_dist(symbol, tick, info)
                        if target_sl < min_sl:
                            target_sl = min_sl
                        self._modify_sl_tp(pos, target_sl, pos.tp)
                        trade_data["be_done"] = True
                        logger.info(f"EARLY BREAKEVEN [{symbol}] SL moved to entry (trigger {be_trigger})")
                    elif pos.type == 1 and current_price <= trade_data["entry_price"] - be_trigger * step_dist:
                        target_sl = round(pos.price_open - (10 ** -digits), digits)
                        min_sl = current_price + self._get_min_dist(symbol, tick, info)
                        if target_sl > min_sl:
                            target_sl = min_sl
                        self._modify_sl_tp(pos, target_sl, pos.tp)
                        trade_data["be_done"] = True
                        logger.info(f"EARLY BREAKEVEN [{symbol}] SL moved to entry (trigger {be_trigger})")

            # 3-LEG BE RETRY (audit 2026-08-18, XAGUSD group): after leg 1 was
            # closed by the broker TP1, the SL of legs 2/3 must be pulled toward
            # entry. The close-detector block below attempts that move at the
            # moment of the close; when the broker min-stop distance blocks a
            # true breakeven (e.g. SILVER: SL must stay >= bid + 20 pts, so a
            # breakeven is only reachable after the price retraces past it),
            # be_done stays False and this block retries EVERY BE CHECK, moving
            # the SL as tight as the market allows (ratcheting up as bid/ask
            # drift in our favour) until a true breakeven or a position close.
            if trade_data.get("leg") in (2, 3) and not trade_data.get("be_done", False):
                group_key = trade_data.get("group_key")
                entry = trade_data.get("entry_price")
                if group_key and entry is not None:
                    leg1_open = any(
                        t is not trade_data
                        and t.get("group_key") == group_key
                        and t.get("leg") == 1
                        for t in self.active_trades.values()
                    )
                    if not leg1_open and self._move_sl_to_entry(pos, entry):
                        trade_data["be_done"] = True

            # CALIBRATION 2026-08-21: PROFIT TRAILING — after breakeven (BE done),
            # trail the stop into profit. The SL ratchets in our favor, locking
            # in lock_pct of unrealized profit. Never moves back.
            # LONG: trail_sl moves UP (closer to current price) = tighter.
            # SHORT: trail_sl moves DOWN (closer to current price) = tighter.
            if trade_data.get("be_done", False) and not trade_data.get("trailing_active", False):
                profit_trail_cfg = self._get_profit_trail_config(symbol)
                if profit_trail_cfg:
                    activation_atr = profit_trail_cfg.get("activation_atr", 0.5)
                    lock_pct = profit_trail_cfg.get("lock_pct", 0.60)
                    min_profit_price = profit_trail_cfg.get("min_profit_price", 0)
                    try:
                        atr_now = self._latest_causal_atr(trade_data.get("symbol", symbol))
                    except Exception:
                        atr_now = 0
                    entry_price = trade_data.get("entry_price", pos.price_open)
                    if pos.type == 0:  # LONG
                        unrealized = current_price - entry_price
                        activation_dist = activation_atr * atr_now if atr_now > 0 else min_profit_price
                        if unrealized > max(activation_dist, min_profit_price):
                            # LONG trail: SL moves UP toward current price, locking profit
                            trail_sl = round(current_price - unrealized * (1 - lock_pct), digits)
                            min_sl = current_price - self._get_min_dist(symbol, tick, info)
                            if trail_sl < min_sl:
                                trail_sl = min_sl
                            if trail_sl > pos.sl:
                                self._modify_sl_tp(pos, trail_sl, pos.tp)
                                trade_data["trailing_active"] = True
                                logger.info(
                                    f"PROFIT TRAIL [{symbol}] LONG: SL {pos.sl} -> {trail_sl} "
                                    f"(profit={unrealized:.5f}, locked={unrealized*lock_pct:.5f})"
                                )
                    else:  # SHORT
                        unrealized = entry_price - current_price
                        activation_dist = activation_atr * atr_now if atr_now > 0 else min_profit_price
                        if unrealized > max(activation_dist, min_profit_price):
                            # SHORT trail: SL moves DOWN toward current price, locking profit
                            trail_sl = round(current_price + unrealized * (1 - lock_pct), digits)
                            min_sl = current_price + self._get_min_dist(symbol, tick, info)
                            if trail_sl > min_sl:
                                trail_sl = min_sl
                            if trail_sl < pos.sl:
                                self._modify_sl_tp(pos, trail_sl, pos.tp)
                                trade_data["trailing_active"] = True
                                logger.info(
                                    f"PROFIT TRAIL [{symbol}] SHORT: SL {pos.sl} -> {trail_sl} "
                                    f"(profit={unrealized:.5f}, locked={unrealized*lock_pct:.5f})"
                                )

            # Частичные закрытия. W2: each tranche is quantized to the broker's
            # volume_step/volume_min (so a 0.01 base lot no longer closes the whole
            # position as "TP1 (50%)" or issues a zero-volume TP2 order); a tranche
            # below the minimum is skipped and the remainder stays on the broker TP.
            # W12: tp1_hit/tp2_hit advance only after the partial close is accepted.
            # 3-LEG path (trade_data["leg"] set): each leg carries its OWN broker
            # TP (TP1/TP2/TP3), so the broker closes it — the partial-close ladder
            # below applies only to legacy single-position trades (leg is None).
            if trade_data.get("leg") is None:
                if pos.type == 0:
                    if tp1 is not None and not tp1_hit and current_price >= tp1:
                        close_vol = self._scaleout_volume(symbol, info, original_volume, 0.5)
                        if close_vol > 0:
                            if self._close_partial_position(pos, tick.bid, close_vol, "TP1 (50%)"):
                                trade_data["tp1_hit"] = True
                                target_sl = round(pos.price_open + (10 ** -digits), digits)
                                min_sl = current_price - self._get_min_dist(symbol, tick, info)
                                if target_sl < min_sl:
                                    target_sl = min_sl
                                self._modify_sl_tp(pos, target_sl, pos.tp)
                    elif tp2 is not None and tp1_hit and not tp2_hit and current_price >= tp2:
                        close_vol = self._scaleout_volume(symbol, info, original_volume, 0.3)
                        if close_vol > 0 and self._close_partial_position(pos, tick.bid, close_vol, "TP2 (30%)"):
                            trade_data["tp2_hit"] = True
                    elif tp3 is not None and tp2_hit and current_price >= tp3:
                        self._close_partial_position(pos, tick.bid, pos.volume, "TP3 (20%)")

                    # v4b TRAILING after TP2 (only if trailing_atr_mult set and not yet trailed)
                    trailing_mult = self.trailing_atr_mult_by_symbol.get(symbol)
                    if trailing_mult is not None and tp1_hit and tp2_hit and not trade_data.get("trailing_active", False):
                        try:
                            atr_now = self._latest_causal_atr(trade_data.get("symbol", symbol))
                            trail_dist = float(trailing_mult) * atr_now
                            if pos.type == 0:
                                new_sl = round(pos.price_open + (current_price - pos.price_open) * 0.7, digits)  # conservative
                                # Prefer dynamic trail using recent high
                                if current_price > pos.price_open:
                                    new_sl = round(current_price - trail_dist, digits)
                                if new_sl > pos.sl:
                                    min_sl = current_price - self._get_min_dist(symbol, tick, info)
                                    if new_sl >= min_sl:
                                        self._modify_sl_tp(pos, new_sl, pos.tp)
                                        trade_data["trailing_active"] = True
                            else:
                                new_sl = round(pos.price_open - (pos.price_open - current_price) * 0.7, digits)
                                if current_price < pos.price_open:
                                    new_sl = round(current_price + trail_dist, digits)
                                if new_sl < pos.sl:
                                    min_sl = current_price + self._get_min_dist(symbol, tick, info)
                                    if new_sl <= min_sl:
                                        self._modify_sl_tp(pos, new_sl, pos.tp)
                                        trade_data["trailing_active"] = True
                        except Exception as exc:
                            logger.error("[%s] trailing skipped: causal ATR unavailable: %s", symbol, exc)
                else:
                    if tp1 is not None and not tp1_hit and current_price <= tp1:
                        close_vol = self._scaleout_volume(symbol, info, original_volume, 0.5)
                        if close_vol > 0:
                            if self._close_partial_position(pos, tick.ask, close_vol, "TP1 (50%)"):
                                trade_data["tp1_hit"] = True
                                target_sl = round(pos.price_open - (10 ** -digits), digits)
                                min_sl = current_price + self._get_min_dist(symbol, tick, info)
                                if target_sl > min_sl:
                                    target_sl = min_sl
                                self._modify_sl_tp(pos, target_sl, pos.tp)
                    elif tp2 is not None and tp1_hit and not tp2_hit and current_price <= tp2:
                        close_vol = self._scaleout_volume(symbol, info, original_volume, 0.3)
                        if close_vol > 0 and self._close_partial_position(pos, tick.ask, close_vol, "TP2 (30%)"):
                            trade_data["tp2_hit"] = True
                    elif tp3 is not None and tp2_hit and current_price <= tp3:
                        self._close_partial_position(pos, tick.ask, pos.volume, "TP3 (20%)")

        # W10: persist any management-state changes (partial closes, BE, trailing)
        # so a restart keeps managing open positions correctly.
        self._save_management_state()

        # Детектор закрытия. Срабатывает и когда закрылась одна позиция из
        # нескольких, и — после фикса выше — когда закрылась ПОСЛЕДНЯЯ (тогда
        # positions пуст и current_tickets пуст, значит все отслеживаемые тикеты
        # попадают сюда и уведомление в Telegram уходит).
        closed_tickets = set(self.active_trades.keys()) - current_tickets
        for ticket in closed_tickets:
            trade_info = self.active_trades.pop(ticket, {})
            symbol = trade_info.get("symbol", "ASSET")
            # A failing history lookup must never abort the close handling of
            # the remaining tickets (nor the Telegram notification below).
            try:
                history_deals = mt5.history_deals_get(position=ticket)
            except Exception as e:
                logger.error(f"history_deals_get failed for #{ticket}: {e}")
                history_deals = None
            total_pnl = 0.0
            if history_deals:
                total_pnl = sum(d.profit + d.swap + d.commission for d in history_deals)

            # CRIT 5: persist the realized close to the executed_trades log.
            # Prefer the exact deal (entry=OUT) times/prices for accuracy; fall
            # back on the last deal and our last known current price.
            close_time = int(datetime.now(timezone.utc).timestamp())
            close_price = trade_info.get("entry_price")
            deal_out = [d for d in (history_deals or []) if getattr(d, "entry", None) == 1]
            if deal_out:
                close_time = int(getattr(deal_out[-1], "time", close_time))
                close_price = float(deal_out[-1].price)
            elif history_deals:
                close_time = int(getattr(history_deals[-1], "time", close_time))
                close_price = float(getattr(history_deals[-1], "price", close_price or 0.0) or close_price or 0.0)
            # pnl from history_deals (money, broker-adjusted) - most accurate.
            realized_pnl = total_pnl if history_deals else self.last_close_pnl.get(ticket, 0.0)
            if close_price is None:
                close_price = 0.0
            try:
                self._append_trade_event(
                    "position_closed", symbol, trade_info.get("signal_contract") or {},
                    position_ticket=ticket, reason="broker_position_closed",
                    payload={"close_time": close_time, "close_price": close_price,
                             "realized_pnl": realized_pnl}, actor="broker_history",
                )
                log_trade_close(
                    self.trade_db_path,
                    ticket,
                    close_time,
                    close_price,
                    realized_pnl,
                )
            except Exception as e:
                logger.error(f"Trade close logging failed for #{ticket}: {e}")
            finally:
                self.signal_features.pop(ticket, None)
                self.last_close_pnl.pop(ticket, None)
                try:
                    purge_closed_position_context(ticket)
                except Exception as e:
                    logger.error(f"Position context purge failed for #{ticket}: {e}")

            if total_pnl < 0:
                self.streak_losses[symbol] = self.streak_losses.get(symbol, 0) + 1
            else:
                self.streak_losses[symbol] = 0

            status_emoji = "💵 PROFIT" if total_pnl >= 0 else "🛑 LOSS/BREAKEVEN"
            leg_label = f" (Leg {trade_info.get('leg')})" if trade_info.get("leg") else ""
            close_msg = (
                f"✅ [{symbol}] TRADE CLOSED #{ticket}{leg_label}\n"
                f"Result: {status_emoji}\n"
                f"Total PnL: ${realized_pnl:+.2f}\n"
                f"Loss streak: {self.streak_losses.get(symbol, 0)}"
            )
            logger.info(close_msg)
            self.bot.send_text_message(close_msg)

            # 3-LEG BE: the TP1 leg closed (broker TP1 fill) -> move the SL of
            # the remaining legs of the same group to entry.
            if trade_info.get("leg") == 1:
                group_key = trade_info.get("group_key")
                entry_price = trade_info.get("entry_price")
                if group_key and entry_price is not None:
                    pos_by_ticket = {}
                    for pos in (positions or []):
                        try:
                            pos_by_ticket[int(getattr(pos, "ticket", 0))] = pos
                        except (TypeError, ValueError):
                            continue
                    for other_ticket, other in list(self.active_trades.items()):
                        if (other.get("group_key") == group_key
                                and other.get("leg") in (2, 3)
                                and not other.get("be_done")):
                            pos = pos_by_ticket.get(int(other_ticket))
                            if pos is None:
                                continue
                            # be_done is set only when the move was ACCEPTED by
                            # the broker (audit 2026-08-18: it was set
                            # unconditionally, so a blocked breakeven was never
                            # retried and the legs died at the original SL).
                            if self._move_sl_to_entry(pos, entry_price):
                                other["be_done"] = True

        # W10: reflect closed-ticket removal in the persisted management state.
        self._save_management_state()

    def _get_profit_trail_config(self, symbol: str) -> dict | None:
        """Read profit_trail config for a symbol from signal_grid.
        Returns dict with activation_atr, lock_pct, min_profit_price or None.
        """
        for a_cfg in self.cfg.get("assets", {}).values():
            if a_cfg.get("mt5_symbol") == symbol:
                grid = a_cfg.get("signal_grid", {})
                pt = grid.get("profit_trail")
                if pt:
                    return pt
                break
        # Fallback to global signal_grid
        global_grid = self.cfg.get("signal_grid", {})
        return global_grid.get("profit_trail")

    def _latest_causal_atr(self, asset_key: str) -> float:
        pipeline = self.pipelines.get(asset_key)
        if pipeline is None:
            raise RuntimeError(f"no live pipeline for ATR: {asset_key}")
        frame = pipeline.get_frame(n_candles=120, build_features=True)
        atr = float(frame["atr"].iloc[-1])
        if not math.isfinite(atr) or atr <= 0:
            raise RuntimeError(f"invalid live ATR for {asset_key}: {atr}")
        return atr

    def _get_min_dist(self, symbol: str, tick, info) -> float:
        stops_level = info.trade_stops_level * info.point
        freeze_level = info.trade_freeze_level * info.point
        spread = abs(tick.ask - tick.bid)
        return max(stops_level, freeze_level, spread + 30 * info.point)

    def _be_min_dist(self, info) -> float:
        """Tightest SL distance the broker allows for a breakeven move.

        Only the symbol's own trade_stops_level / trade_freeze_level (no
        self-imposed spread buffer — audit 2026-08-18: the +30 pts buffer in
        _get_min_dist was ~2.5x the real SILVER minimum and made a breakeven
        move look broker-blocked). Per MT5 semantics the distance is measured
        from the BID for sell positions and the ASK for buy positions; the
        caller applies it on the correct side.
        """
        point = getattr(info, "point", 0) or 0
        stops_level = getattr(info, "trade_stops_level", 0) or 0
        freeze_level = getattr(info, "trade_freeze_level", 0) or 0
        return max(stops_level, freeze_level) * point

    def _scaleout_volume(self, symbol: str, info, original_volume: float,
                         ratio: float) -> float:
        """Tranche volume to close, quantized to the broker's lot step.

        W2/N11 (audit 2026-08-10): the previous code did `round(original*0.5, 2)`,
        which for a 0.01 base lot produces 0.01 (closing the WHOLE position as
        "TP1 (50%)") and for TP2 produces 0.0 (a zero-volume order). MT5 requires
        volume to be a multiple of `volume_step` and >= `volume_min`, so we
        round DOWN to the lot step and return 0 (skip the tranche, keep the
        remainder managed by the broker TP) when the tranche is below the
        minimum fillable volume.
        """
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        min_lot = float(getattr(info, "volume_min", step) or step)
        raw = original_volume * ratio
        tranche = math.floor(raw / step) * step
        if tranche < min_lot - 1e-9 or tranche <= 0.0:
            logger.warning(
                f"[{symbol}] Scale-out tranche {ratio} of {original_volume} "
                f"lots = {raw:.4f} < min fillable {min_lot:.2f} (step {step:.2f}); "
                f"skipping partial close and keeping remainder on the broker TP."
            )
            return 0.0
        return round(tranche, 6)

    # ------------------------------------------------------------------
    # 3-LEG OPEN (hedging accounts): one signal -> three separate market
    # orders, each with its own attached TP (TP1/TP2/TP3) and the shared SL.
    # The broker closes each leg at its own TP; the bot only manages the
    # breakeven move of the remaining legs once the TP1 leg is closed.
    # ------------------------------------------------------------------
    SCALEOUT_RATIOS = ((1 / 3, 1), (1 / 3, 2), (1 / 3, 3))  # (ratio, leg_no)

    def _leg_volumes(self, info) -> list[float]:
        """Equal-split 3-leg volumes (leg3 absorbs the lot-step remainder, so
        the legs always sum exactly to self.volume). Legs below the fillable
        minimum are reported as 0.0 and skipped by the caller."""
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        min_lot = float(getattr(info, "volume_min", step) or step)
        each = math.floor(self.volume / 3 / step) * step
        vols = [
            round(each, 6),
            round(each, 6),
            round(self.volume - 2 * each, 8),
        ]
        for i, v in enumerate(vols, 1):
            if v > 0.0 and v < min_lot - 1e-9:
                logger.warning(
                    f"Leg {i} volume {v:.4f} < min fillable {min_lot:.2f} "
                    f"(step {step:.2f}); skipping this leg."
                )
        return vols

    def _normalize_tp_level(self, symbol: str, side: str, raw_tp: float) -> float:
        """Clamp a leg TP to the broker minimum distance (same math as
        _normalize_stops uses for the TP side)."""
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if not info or not tick:
            raise RuntimeError(f"symbol_info failed for {symbol}")
        point = info.point
        stops_level = info.trade_stops_level
        freeze_level = info.trade_freeze_level
        spread = abs(tick.ask - tick.bid)
        min_dist = max(stops_level, freeze_level, int(round(spread / point)) + 30) * point
        digits = info.digits
        if side == "long":
            tp = max(raw_tp, tick.ask + min_dist)
        else:
            tp = min(raw_tp, tick.bid - min_dist)
        return round(round(tp / point) * point, digits)

    def _move_sl_to_entry(self, pos, entry_price: float = None) -> bool:
        """Move a position's SL to (just past) its entry price.

        Returns True when the SL now sits at the desired entry target;
        False when the broker's minimum stop distance forced a tighter
        clamp (the SL is still improved if allowed) — the caller then
        keeps retrying until the position closes or a true breakeven
        becomes reachable.

        Audit 2026-08-18 (XAGUSD group): the old code returned silently
        whenever the entry target was closer than _get_min_dist() (which
        padded the distance with spread + 30 pts), and the 3-LEG BE block
        set be_done=True regardless, so the SL of legs 2/3 never moved and
        they were stopped out at the original SL. Now the minimum distance
        uses only the broker's own stops/freeze level measured from the
        fill side (bid for sells, ask for buys), the target is clamped
        instead of abandoned and acceptance is reported via the return
        value.
        """
        try:
            tick = mt5.symbol_info_tick(pos.symbol)
            info = mt5.symbol_info(pos.symbol)
            if not tick or not info:
                return False
            digits = info.digits
            tick_size = 10 ** -digits
            anchor = entry_price if entry_price is not None else pos.price_open
            be_dist = self._be_min_dist(info)
            current_sl = float(getattr(pos, "sl", 0) or 0)
            if pos.type == 0:
                target_sl = round(anchor + tick_size, digits)
                min_sl = round(tick.ask - be_dist, digits)
                clamped = target_sl > min_sl
                if clamped:
                    target_sl = min_sl
                if current_sl > 0 and target_sl <= current_sl:
                    logger.debug(
                        f"BREAKEVEN [{pos.symbol}] #{pos.ticket} SL already at best "
                        f"possible {current_sl} (entry {anchor}, min {min_sl})"
                    )
                    return False
            else:
                target_sl = round(anchor - tick_size, digits)
                min_sl = round(tick.bid + be_dist, digits)
                clamped = target_sl < min_sl
                if clamped:
                    target_sl = min_sl
                if current_sl > 0 and target_sl >= current_sl:
                    logger.debug(
                        f"BREAKEVEN [{pos.symbol}] #{pos.ticket} SL already at best "
                        f"possible {current_sl} (entry {anchor}, min {min_sl})"
                    )
                    return False
            ok = self._modify_sl_tp(pos, target_sl, getattr(pos, "tp", None))
            if ok and not clamped:
                logger.info(
                    f"BREAKEVEN [{pos.symbol}] #{pos.ticket} SL moved to entry "
                    f"after TP1 leg close"
                )
            elif ok:
                logger.info(
                    f"BREAKEVEN [{pos.symbol}] #{pos.ticket} SL pulled to broker "
                    f"minimum {target_sl} (entry {anchor} unreachable: min distance "
                    f"{be_dist:.{digits}f}); will retry to tighten"
                )
            # Clamped (or rejected) moves report False so the caller keeps
            # retrying: the SL ratchets tighter as bid/ask drift in our favour
            # until a true breakeven becomes reachable.
            return ok and not clamped
        except Exception as exc:
            logger.warning(f"Breakeven move failed for #{getattr(pos, 'ticket', '?')}: {exc}")
            return False

    def _append_trade_event(self, event_type: str, asset_key: str, signal: dict,
                            *, position_ticket=None, order_ticket=None, reason=None,
                            payload=None, actor="mt5_trader"):
        try:
            append_trading_event(
                self.trade_db_path, event_type=event_type,
                signal_id=str(signal.get("signal_id") or f"legacy:{asset_key}:{signal.get('timestamp_utc', 0)}"),
                asset_key=asset_key,
                strategy_version=str(signal.get("strategy_version") or self.strategy_identity["strategy_version"]),
                config_hash=str(signal.get("config_hash") or self.strategy_identity["config_hash"]),
                model_hash=signal.get("model_hash"),
                feature_snapshot_hash=signal.get("feature_snapshot_hash"),
                position_ticket=position_ticket, order_ticket=order_ticket,
                actor=actor, reason=reason, payload=payload or {},
            )
        except Exception as exc:
            logger.error("Trading event ledger write failed: %s", exc)

    def _account_fingerprint(self) -> str:
        """Best-effort '<mode>:<login>' fingerprint used in deterministic event ids."""
        try:
            account = mt5.account_info()
            trade_mode = getattr(account, "trade_mode", None)
            login = int(getattr(account, "login", 0) or 0)
            mode = "unknown"
            for attr, label in (("ACCOUNT_TRADE_MODE_DEMO", "demo"),
                                ("ACCOUNT_TRADE_MODE_CONTEST", "contest"),
                                ("ACCOUNT_TRADE_MODE_REAL", "real")):
                value = getattr(mt5, attr, None)
                if value is not None and trade_mode == value:
                    mode = label
                    break
            return account_fingerprint(mode, login)
        except Exception:  # pragma: no cover - defensive
            return account_fingerprint("unknown", 0)

    def _enqueue_execution_fact(self, event: ExecutionEvent) -> None:
        """Durable outbox enqueue; never raises into the trading path."""
        try:
            enqueue_event(self.trade_db_path, event)
        except Exception as exc:
            logger.error("Ledger outbox enqueue failed: %s", exc)

    def _intent_created_fact(self, intent) -> ExecutionEvent:
        fp = self._account_fingerprint()
        return ExecutionEvent(
            event_id=execution_event_id("mt5_python_sender", fp, "intent", intent.intent_id),
            event_type="intent_created",
            intent_id=intent.intent_id,
            source="mt5_python_sender",
            account_mode=fp.split(":", 1)[0],
            broker_symbol=intent.broker_symbol,
            asset_key=intent.asset_key,
            magic_number=intent.magic_number,
            volume_requested=intent.requested_volume,
            precision="request",
            received_at_utc_ms=now_ms(),
            payload={"signal_id": intent.signal_id, "mode": intent.mode},
        )

    def _request_result_fact(self, *, intent_id, asset_key, broker_symbol, request,
                             result, requested_at_ms) -> ExecutionEvent:
        fp = self._account_fingerprint()
        result_order = int(getattr(result, "order", 0) or 0)
        result_deal = int(getattr(result, "deal", 0) or 0)
        if result_order or result_deal:
            tx_id = f"{result_order}:{result_deal}"
        else:
            tx_id = f"t:{int(requested_at_ms)}:{int(getattr(result, 'retcode', 0) or 0)}"
        retcode = getattr(result, "retcode", None)
        done_codes = {
            code for code in (
                getattr(mt5, "TRADE_RETCODE_DONE", None),
                getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", None),
            ) if code is not None
        }
        is_done = retcode in done_codes
        fill_price = getattr(result, "price", None) if is_done else None
        if fill_price is not None and float(fill_price) <= 0:
            fill_price = None
        filled_volume = getattr(result, "volume", None) if is_done else None
        if filled_volume is not None and float(filled_volume) <= 0:
            filled_volume = None
        return ExecutionEvent(
            event_id=execution_event_id("mt5_python_sender", fp, "request", tx_id),
            event_type="request_result",
            intent_id=intent_id,
            source="mt5_python_sender",
            account_mode=fp.split(":", 1)[0],
            broker_symbol=broker_symbol,
            asset_key=asset_key,
            magic_number=request.get("magic"),
            order_ticket=result_order or None,
            deal_ticket=result_deal or None,
            retcode=retcode,
            requested_price=request.get("price"),
            fill_price=fill_price,
            filled_volume=filled_volume,
            volume_requested=request.get("volume"),
            latency_ms=max(0, now_ms() - int(requested_at_ms)),
            precision="request",
            received_at_utc_ms=now_ms(),
            reason=None if is_done else str(getattr(result, "comment", "") or ""),
            payload={"action": str(request.get("action", "")),
                     "order_comment": str(request.get("comment", ""))},
        )

    def _record_execution_result(self, *, asset_key, broker_symbol, action, side,
                                 requested_at, request, result,
                                 position_ticket=None, intent_id=None,
                                 precision="request"):
        """Best-effort append-only execution telemetry; never blocks trading."""
        try:
            done_codes = {
                code for code in (
                    getattr(mt5, "TRADE_RETCODE_DONE", None),
                    getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", None),
                ) if code is not None
            }
            retcode = getattr(result, "retcode", None)
            is_done = retcode in done_codes
            status = (
                "partial" if retcode == getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", object())
                else "filled" if is_done else "rejected"
            )
            result_price = getattr(result, "price", None) if is_done else None
            if result_price is not None and float(result_price) <= 0:
                result_price = None  # some MT5 result types report 0; never invent slippage
            result_volume = getattr(result, "volume", None) if is_done else None
            if result_volume is not None and float(result_volume) <= 0:
                result_volume = None
            log_execution_attempt(
                self.trade_db_path,
                asset_key=str(asset_key),
                broker_symbol=str(broker_symbol),
                action=action,
                side=side,
                requested_at_ms=requested_at,
                completed_at_ms=now_ms(),
                requested_price=request.get("price"),
                filled_price=result_price,
                volume_requested=request.get("volume"),
                volume_filled=result_volume,
                status=status,
                retcode=retcode,
                rejection_reason=None if is_done else str(getattr(result, "comment", "")),
                order_ticket=getattr(result, "order", None),
                position_ticket=position_ticket,
                intent_id=intent_id,
                precision=precision,
                metadata={"comment": request.get("comment")},
            )
        except Exception as exc:
            logger.error("Execution ledger write failed: %s", exc)

    def _modify_sl_tp(self, pos, new_sl, new_tp):
        """Send a TRADE_ACTION_SLTP modify; returns True only when the broker
        accepted it (or when running in dry-run). Audit 2026-08-18: breakeven
        logic now keys on the acceptance, not on the send attempt."""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "sl": new_sl,
            "tp": new_tp,
        }
        if self.dry_run:
            logger.info(f"[DRY RUN] Would modify SL/TP: {request}")
            return True
        trade = self.active_trades.get(pos.ticket, {})
        signal = trade.get("signal_contract") or {}
        asset_key = trade.get("symbol", pos.symbol)
        self._append_trade_event("stop_move_requested", asset_key, signal,
                                 position_ticket=pos.ticket,
                                 payload={"old_sl": getattr(pos, "sl", None), "new_sl": new_sl,
                                          "old_tp": getattr(pos, "tp", None), "new_tp": new_tp})
        res = mt5.order_send(request)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self._append_trade_event("stop_move_confirmed", asset_key, signal,
                                     position_ticket=pos.ticket, reason="broker_confirmed",
                                     payload={"new_sl": new_sl, "new_tp": new_tp})
            logger.info(f"✅ Modified SL/TP for #{pos.ticket}: SL={new_sl}, TP={new_tp}")
            return True
        else:
            self._append_trade_event("stop_move_rejected", asset_key, signal,
                                     position_ticket=pos.ticket,
                                     reason=str(getattr(res, "comment", "rejected")),
                                     payload={"retcode": getattr(res, "retcode", None)})
            logger.debug(f"Modify failed: {res.comment} ({res.retcode})")
            return False

    def _close_partial_position(self, pos, price, volume, label):
        """Close a partial volume. Returns True only if the order was accepted
        (retcode == TRADE_RETCODE_DONE).

        W12 (audit 2026-08-10): callers previously set tp1_hit/tp2_hit BEFORE
        checking the retcode, so a rejected partial (requote, trade_freeze,
        volume) was forever treated as executed — leaving the position full-size,
        without breakeven and without a retry. Success is now signalled by the
        return value and the state is advanced only on real execution.
        """
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": f"{label} close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if self.dry_run:
            logger.info(f"[DRY RUN] Would close partial {label}: {request}")
            return True  # dry-run: treated as filled for state-advance purposes
        trade = self.active_trades.get(pos.ticket, {})
        asset_key = trade.get("symbol", pos.symbol)
        signal = trade.get("signal_contract") or {}
        self._append_trade_event("partial_close_submitted", asset_key, signal,
                                 position_ticket=pos.ticket,
                                 reason=label, payload={"price": price, "volume": volume})
        requested_at = now_ms()
        res = mt5.order_send(request)
        self._record_execution_result(
            asset_key=asset_key,
            broker_symbol=pos.symbol,
            action="partial_close",
            side="sell" if pos.type == 0 else "buy",
            requested_at=requested_at,
            request=request,
            result=res,
            position_ticket=pos.ticket,
        )
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self._append_trade_event("partial_filled", asset_key, signal,
                                     position_ticket=pos.ticket,
                                     order_ticket=getattr(res, "order", None), reason=label,
                                     payload={"requested_price": price,
                                              "filled_price": getattr(res, "price", None),
                                              "volume": volume})
            logger.info(f"✅ Closed {label} for #{pos.ticket} ({volume} lots)")
            return True
        self._append_trade_event("partial_rejected", asset_key, signal,
                                 position_ticket=pos.ticket, reason=str(res.comment),
                                 payload={"retcode": res.retcode, "label": label})
        logger.error(f"❌ Partial close failed {label} for #{pos.ticket}: {res.comment} ({res.retcode})")
        return False

    def execute_signal(self, asset_key: str, signal: dict):
        bias = signal["bias"]
        if bias == "no_trade":
            return
        # Blackout guard (owner request 2026-08-19): never open positions
        # during market inactivity even if a signal slipped through.
        now_utc = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        halted, halt_reason, _ = self._blackout_status(now_utc)
        if halted or self._in_daily_break(now_utc):
            logger.info(f"[{asset_key}] Signal skipped: market "
                        f"{halt_reason or 'in daily break'}")
            return
        signal_id = str(signal.get("signal_id") or f"legacy:{asset_key}:{signal.get('timestamp_utc', 0)}")
        allowed, deployment_reason = order_routing_allowed(
            self.cfg, confirmed_by=signal.get("confirmed_by")
        )
        if not self.order_routing_enabled or not allowed:
            logger.warning("[%s] Order routing blocked: %s", asset_key, deployment_reason)
            return
        if asset_key not in self.execution_assets:
            logger.warning("[%s] Order routing blocked by execution allowlist", asset_key)
            return
        if int(signal.get("expires_at_utc") or 0) and int(time.time()) > int(signal["expires_at_utc"]):
            logger.warning("[%s] Signal %s expired before execution", asset_key, signal_id)
            return
        if signal.get("signal_state") not in {None, "confirmed"}:
            logger.warning("[%s] Signal %s is not confirmed", asset_key, signal_id)
            return

        min_conf = self._get_dynamic_min_confidence(asset_key)
        if signal["confidence"] < min_conf:
            logger.info(f"[{asset_key}] Signal suppressed by dynamic threshold: conf={signal['confidence']:.3f} < {min_conf:.3f}")
            return

        # Корреляционный фильтр
        if self._has_correlated_position(asset_key, bias):
            logger.info(f"[{asset_key}] Blocked by correlation filter.")
            return

        # Risk budget counted per GROUP (audit 2026-08-19, owner request): a
        # 3-leg group consumes ONE slot, so the 6-slot budget covers 6 ASSETS
        # instead of 2 (previously legs 2+3 of the second asset hit
        # "Max concurrent positions limit reached (6/6)" and blocked
        # high-confidence signals on the next asset).
        positions_now = positions_get_by_magic(magic=self.magic_number)
        if positions_now is None:
            positions_now = []
        groups_by_asset, singles_by_asset = self._group_position_counts(positions_now)
        can_trade, reason = self.risk_manager.can_trade(
            asset_key, groups_by_asset, singles_by_asset)
        if not can_trade:
            logger.warning(f"Trade suppressed for {asset_key} by Risk Manager: {reason}")
            return

        mt5_symbol = self.cfg["assets"][asset_key]["mt5_symbol"]
        if not mt5.initialize():
            return
        validate_symbol(mt5_symbol)

        positions = positions_get_by_magic(symbol=mt5_symbol, magic=self.magic_number)
        if positions:
            return

        tick = mt5.symbol_info_tick(mt5_symbol)
        info = mt5.symbol_info(mt5_symbol)
        if not tick or not info:
            return

        order_type = mt5.ORDER_TYPE_BUY if bias == "long" else mt5.ORDER_TYPE_SELL
        price = tick.ask if bias == "long" else tick.bid

        # Recenter the frozen signal-bar grid on the actual requested entry.
        # Using absolute levels built around the previous close makes live gap
        # entries differ from the next-open backtest and paper accumulator.
        direction = 1 if bias == "long" else -1
        step = float(signal.get("step") or 0.0)
        if step <= 0:
            logger.error("[%s] Signal has no positive grid step; refusing order", asset_key)
            return
        regime_name = str(signal.get("regime", ""))
        grid = get_signal_grid(self.cfg, self.cfg["assets"][asset_key], regime=regime_name)
        targets = [
            price + direction * step * float(grid.get(key, default))
            for key, default in (("tp1_mult", 1.0), ("tp2_mult", 2.0), ("tp3_mult", 3.0))
        ]
        invalidation = price - direction * step * float(grid.get("stop_mult", 2.0))
        raw_tp = float(targets[2])

        try:
            sl_price, tp_price = self._normalize_stops(mt5_symbol, bias, price, invalidation, raw_tp)
        except Exception as e:
            logger.error(f"Normalization failed for {mt5_symbol}: {e}")
            return
        digits = int(getattr(info, "digits", 5))
        targets = [round(float(t), digits) for t in targets]
        targets[2] = float(tp_price)
        invalidation = float(sl_price)
        signal = dict(signal)
        signal.update({
            "entry_zone": [round(price - step * 0.1, digits), round(price + step * 0.1, digits)],
            "invalidation": invalidation,
            "targets": targets,
        })

        # 3-LEG OPEN (hedging accounts): the signal's total volume is split
        # EQUALLY into three market orders (leg3 carries the lot-step
        # remainder), each with its own TP (TP1/TP2/TP3) and the shared SL.
        # The broker closes each leg at its own TP; the bot only moves the
        # remaining legs' SL to entry once the TP1 leg closes (see
        # check_and_move_breakeven).
        leg_tps = [
            self._normalize_tp_level(mt5_symbol, bias, targets[0]),
            self._normalize_tp_level(mt5_symbol, bias, targets[1]),
            self._normalize_tp_level(mt5_symbol, bias, targets[2]),
        ]

        if self.dry_run:
            for leg_volume, (_, leg_no) in zip(self._leg_volumes(info), self.SCALEOUT_RATIOS):
                if leg_volume <= 0:
                    continue
                logger.info(
                    f"[DRY RUN] Leg {leg_no} order NOT sent for {asset_key}: "
                    f"volume={leg_volume}, SL={sl_price}, TP={leg_tps[leg_no - 1]}"
                )
            return

        # Wave-0 contract (MQL5 observer plan): persist the immutable
        # SignalIntent BEFORE order_send, then carry a correlation-safe short id
        # in the order comment so the MQL5 observer can join broker deal facts
        # back to this intent. Failures here are logged, never blocking.
        intent = build_signal_intent(
            asset_key=asset_key,
            broker_symbol=mt5_symbol,
            side="long" if bias == "long" else "short",
            requested_volume=self.volume,
            entry_price=price,
            sl_price=sl_price,
            tp_price=leg_tps[2],
            model_version=str(signal.get("model_version") or "") or None,
            feature_manifest_hash=signal.get("feature_manifest_hash"),
            config_hash=signal.get("config_hash") or self.strategy_identity["config_hash"],
            mode=self.deployment_mode.value,
            magic_number=self.magic_number,
            signal_id=signal_id,
            created_at_utc_ms=now_ms(),
        )
        try:
            append_signal_intent(self.trade_db_path, intent)
            self._enqueue_execution_fact(self._intent_created_fact(intent))
        except Exception as exc:
            logger.error("Intent persist/enqueue failed: %s", exc)

        try:
            entry_time = int(float(signal.get("timestamp_utc", 0) or 0))
        except (TypeError, ValueError):
            entry_time = int(datetime.now(timezone.utc).timestamp())

        group_key = f"{asset_key}:{signal_id}"
        opened_any = False
        for leg_volume, (_, leg_no) in zip(self._leg_volumes(info), self.SCALEOUT_RATIOS):
            if leg_volume <= 0:
                continue
            leg_tp = leg_tps[leg_no - 1]
            leg_signal = dict(signal)
            leg_signal["leg"] = leg_no
            # Market orders fill at the CURRENT price; between leg fills the
            # market can move past the signal-time levels and the broker then
            # rejects SL/TP with Retcode 10016 ("Invalid stops"). Recenter each
            # leg's SL/TP on the live price, preserving the exact entry->SL and
            # entry->TP distances of the signal-time geometry.
            leg_sl = sl_price
            try:
                live_tick = mt5.symbol_info_tick(mt5_symbol)
                if live_tick is not None:
                    live_price = float(live_tick.ask if bias == "long" else live_tick.bid)
                    drift = live_price - price
                    leg_sl = round(sl_price + drift, digits)
                    leg_tp = round(leg_tp + drift, digits)
            except Exception as exc:
                logger.warning(
                    "[%s] Live tick unavailable (%s); using signal-time levels", asset_key, exc)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": mt5_symbol,
                "volume": leg_volume,
                "type": order_type,
                "price": price,
                "sl": leg_sl,
                "tp": leg_tp,
                "deviation": 20,
                "magic": self.magic_number,
                "comment": f"{asset_key} ML Scalp L{leg_no} {intent.intent_id[:8]}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            self._append_trade_event(
                "order_submitted", asset_key, leg_signal,
                reason="model_signal_confirmed", payload={
                    "broker_symbol": mt5_symbol, "side": bias, "requested_price": price,
                    "volume": leg_volume, "sl": sl_price, "tp": leg_tp,
                    "deployment_mode": self.deployment_mode.value,
                    "intent_id": intent.intent_id, "leg": leg_no,
                },
            )
            requested_at = now_ms()
            result = mt5.order_send(request)
            self._enqueue_execution_fact(self._request_result_fact(
                intent_id=intent.intent_id, asset_key=asset_key, broker_symbol=mt5_symbol,
                request=request, result=result, requested_at_ms=requested_at,
            ))
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self._append_trade_event(
                    "order_rejected", asset_key, leg_signal,
                    order_ticket=getattr(result, "order", None),
                    reason=str(getattr(result, "comment", "rejected")),
                    payload={"retcode": getattr(result, "retcode", None),
                             "request": request, "leg": leg_no},
                )
                self._record_execution_result(
                    asset_key=asset_key,
                    broker_symbol=mt5_symbol,
                    action="open",
                    side="buy" if bias == "long" else "sell",
                    requested_at=requested_at,
                    request=request,
                    result=result,
                    intent_id=intent.intent_id,
                    precision="request",
                )
                logger.error(
                    f"Order Send Failed for {asset_key} leg {leg_no}: {result.comment} "
                    f"(Retcode: {result.retcode}), request={request}"
                )
                continue

            # HIGH 22: in real MT5 the order ticket (result.order) differs from the
            # position ticket. Resolve the genuine position ticket via positions_get so
            # active_trades and the executed_trades DB log (CRIT 5) are keyed the same
            # way check_and_move_breakeven() sees them (pos.ticket). The pre-check above
            # guarantees no pre-existing open position, so the single/last returned one
            # is the one just opened. The virtual shim returns result.order == pos.ticket
            # on open, so this stays correct (and backward compatible) in both worlds.
            pos_ticket = int(result.order)
            try:
                opened = positions_get_by_magic(symbol=mt5_symbol, magic=self.magic_number)
                if opened:
                    pos_ticket = int(opened[-1].ticket)
            except Exception as e:  # pragma: no cover - defensive fallback
                logger.warning(f"[{asset_key}] Could not resolve position ticket, using order ticket {pos_ticket}: {e}")

            self._append_trade_event(
                "order_filled", asset_key, leg_signal,
                position_ticket=pos_ticket, order_ticket=getattr(result, "order", None),
                reason="broker_fill", payload={
                    "requested_price": price, "filled_price": getattr(result, "price", None),
                    "volume": getattr(result, "volume", leg_volume),
                    "sl": leg_sl, "tp": leg_tp, "leg": leg_no,
                },
            )
            self._record_execution_result(
                asset_key=asset_key,
                broker_symbol=mt5_symbol,
                action="open",
                side="buy" if bias == "long" else "sell",
                requested_at=requested_at,
                request=request,
                result=result,
                position_ticket=pos_ticket,
                intent_id=intent.intent_id,
                precision="request",
            )
            logger.info(
                f"🔥 [{asset_key}] LEG {leg_no}/3 EXECUTED IN MT5! Ticket: #{pos_ticket}, "
                f"Type: {bias.upper()}, Volume: {leg_volume}, Price: {price}, "
                f"SL: {leg_sl}, TP{leg_no}: {leg_tp}"
            )
            opened_any = True

            exec_msg = (
                f"🔥 [{asset_key}] LEG {leg_no}/3 EXECUTED IN MT5!\n"
                f"Ticket: #{pos_ticket}\n"
                f"Type: {bias.upper()}\n"
                f"Volume: {leg_volume} lots\n"
                f"Price: {price}\n"
                f"SL: {leg_sl}\n"
                f"TP{leg_no}: {leg_tp}\n"
                f"Время: {datetime.now(timezone.utc).isoformat()}"
            )
            self.bot.send_text_message(exec_msg)

            self.active_trades[pos_ticket] = {
                "symbol": asset_key,
                "type": bias,
                "entry_price": price,
                "original_volume": leg_volume,
                "leg": leg_no,
                "group_key": group_key,
                "tp1": leg_tp,
                "tp2": None,
                "tp3": None,
                "tp1_hit": False,
                "tp2_hit": False,
                "signal_contract": {
                    k: signal.get(k) for k in (
                        "signal_id", "strategy_version", "config_hash", "model_hash",
                        "feature_snapshot_hash", "timestamp_utc"
                    )
                },
            }

            # CRIT 5: persist the entry so check_and_move_breakeven() can log the close
            # against the same row, feeding scripts/retrain_with_real_trades.py.
            self.signal_features[pos_ticket] = {
                "symbol": asset_key,
                "type": bias,
                "entry_time": entry_time,
                "entry_price": price,
                "features": signal.get("features") or {},
            }
            try:
                log_trade_entry(
                    self.trade_db_path,
                    pos_ticket,
                    asset_key,
                    bias,
                    entry_time,
                    price,
                    signal.get("features") or {},
                )
            except Exception as e:
                logger.error(f"Trade entry logging failed for #{pos_ticket}: {e}")

            # Persist the entry context (bias/confidence/regime/reasoning) keyed by
            # position ticket so the Telegram /status and /why commands can explain
            # the trade later. Read-only side channel; failures must never break
            # the trading path.
            try:
                record_position_context(pos_ticket, asset_key, leg_signal)
            except Exception as e:
                logger.error(f"Position context logging failed for #{pos_ticket}: {e}")

        if opened_any:
            # W10: persist the new legs' management state immediately so a
            # restart before the next BE check still knows their TP targets.
            self._save_management_state()
            self.risk_manager.record_trade_executed(asset_key)
            self.bot.send_alert_if_qualified(signal, asset_key)

    def _validate_contract_sizes(self):
        """T8 (audit 2026-08-10): warn when a symbol's live contract size or
        volume step does not match what the config/backtest assumes. The money
        math (point_value_lot) and the fillable scale-out tranches (volume_step)
        are hard-coded in config; reading them from the terminal catches broker
        or server changes that would otherwise silently shift PnL or block
        partial closes."""
        assets = self.cfg.get("assets", {})
        for asset_key, a_cfg in assets.items():
            symbol = a_cfg.get("mt5_symbol")
            if not symbol:
                continue
            info = mt5.symbol_info(symbol)
            if not info:
                continue
            cfg_pvl = a_cfg.get("point_value_lot")
            live_contract = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
            if cfg_pvl and live_contract > 0 and abs(cfg_pvl - live_contract) > 1e-6:
                logger.warning(
                    f"[{asset_key}] config point_value_lot={cfg_pvl} does not match "
                    f"live trade_contract_size={live_contract} for {symbol}. "
                    f"Money PnL may be mis-scaled."
                )
            live_step = float(getattr(info, "volume_step", 0.0) or 0.0)
            if live_step > 0 and abs(self.volume * 0.5 % live_step) > 1e-9:
                logger.warning(
                    f"[{asset_key}] 50% scale-out of live volume {self.volume} is not a "
                    f"multiple of volume_step {live_step}; partial close may be skipped."
                )

    def run_loop(self):
        initialize_mt5()
        self._validate_contract_sizes()
        self.check_and_move_breakeven()
        logger.info(f"🚀 MULTI-ASSET AUTO-TRADER STARTED WITH FULL TELEGRAM NOTIFICATIONS ({len(self.pipelines)} Assets Enabled)")
        if self.dry_run:
            logger.info("⚠️ DRY RUN MODE - orders will be logged but NOT sent")

        last_bar_time = 0
        last_be_check = 0
        heartbeat = 0
        halted_logged = None

        while True:
            now = time.time()
            now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
            halted, halt_reason, resume_dt = self._blackout_status(now_utc)
            in_daily_break = self._in_daily_break(now_utc)

            if halted:
                if not self._blackout_flattened:
                    try:
                        self._flatten_all_positions(halt_reason)
                    except Exception as e:
                        logger.error(f"Blackout flatten error: {e}")
                    self._blackout_flattened = True
                if halted_logged != halt_reason:
                    halted_logged = halt_reason
                    resume = resume_dt.strftime("%Y-%m-%d %H:%M UTC") if resume_dt else "?"
                    logger.warning(f"⏸ TRADER HALTED (blackout): {halt_reason}; "
                                  f"resumes at {resume}. All trading activity skipped.")
                time.sleep(30)
                continue
            halted_logged = None
            self._blackout_flattened = False

            if now - last_be_check >= 30:
                last_be_check = now
                try:
                    self.check_and_move_breakeven()
                except Exception as e:
                    logger.error(f"Breakeven check error: {e}")

            # Independent cost-calibration samples; never uses an ML signal and
            # remains hard-bounded by the scheduler's session/rate/spread limits.
            try:
                probe = self.fx_probe_scheduler.maybe_run()
                if probe is not None:
                    logger.info("[fx-probe] %s", probe)
            except Exception as e:
                logger.error("[fx-probe] scheduler error: %s", e)

            current_bar_time = 0
            for asset_key, a_cfg in self.cfg["assets"].items():
                if not a_cfg.get("enabled", False) or asset_key not in self.execution_assets:
                    continue
                symbol = a_cfg["mt5_symbol"]
                try:
                    # T11: poll each asset at ITS OWN timeframe (the one it trades
                    # and its pipeline uses), not a global M5.
                    tf_enum = self.symbol_timeframe.get(symbol, mt5.TIMEFRAME_M5)
                    rates = mt5.copy_rates_from_pos(symbol, tf_enum, 1, 1)
                    if rates is not None and len(rates) > 0:
                        t = rates[0]["time"]
                        if t > current_bar_time:
                            current_bar_time = t
                except Exception as e:
                    logger.error(f"Error reading bar time for {symbol}: {e}")

            if current_bar_time == 0:
                time.sleep(2)
                continue

            if in_daily_break:
                # FX/metals server rollover: no bars form and no entries are
                # allowed; the bar pending after the break is analyzed then
                # (last_bar_time stays frozen during the break).
                time.sleep(5)
                continue

            if current_bar_time != last_bar_time:
                logger.info(f"New bar detected: {current_bar_time} (last: {last_bar_time})")
                last_bar_time = current_bar_time
                logger.info("--- Analyzing newly closed candle across all assets ---")
                for asset_key, pipeline in self.pipelines.items():
                    start = time.time()
                    try:
                        signal = pipeline.generate_signal(n_candles=300)
                        elapsed = time.time() - start
                        if signal["bias"] != "no_trade":
                            logger.info(f"[{asset_key}] SIGNAL DETECTED: {signal['bias'].upper()} (Conf: {signal['confidence']}%)")
                            self.execute_signal(asset_key, signal)
                        else:
                            logger.info(f"[{asset_key}] no trade ({elapsed:.2f}s)")
                    except Exception as e:
                        logger.error(f"Error processing {asset_key}: {e} ({time.time()-start:.2f}s)")

            if now - heartbeat >= 60:
                heartbeat = now
                logger.info(f"Waiting for new bar... current_bar_time={current_bar_time}, last={last_bar_time}")

            time.sleep(2)


if __name__ == "__main__":
    trader = MultiAssetMT5Trader()
    # Start the Telegram control/status bot INSIDE the trader process using the
    # same TELEGRAM_BOT_TOKEN as the alerts (no second process, no second token,
    # so no getUpdates conflict). Optional: if the token is not configured the
    # trader keeps running with alert sending only. Do NOT run another polling
    # bot with this token at the same time (e.g. scripts/telegram_admin.py).
    control_bot = None
    try:
        from alerts.control_bot import TelegramControlBot

        control_bot = TelegramControlBot(trader)
        control_bot.start()
    except Exception as e:
        logger.warning(f"Telegram control bot not started ({e}); trader/alerts unaffected.")
        control_bot = None
    try:
        trader.run_loop()
    except KeyboardInterrupt:
        if control_bot is not None:
            control_bot.stop()
        trader.book_feed.stop()
        shutdown_mt5()
        print("Trader stopped safely.")