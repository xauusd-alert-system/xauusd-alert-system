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
from datetime import datetime, timezone
import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, get_env, get_signal_grid
from data.mt5_provider import initialize_mt5, shutdown_mt5, validate_symbol, fetch_closed_candles, _TIMEFRAMES
from data.trade_logger import init_trade_log_schema, log_trade_entry, log_trade_close
from data.execution_ledger import init_execution_ledger, log_execution_attempt, now_ms
from realtime.pipeline import RealtimePipeline
from alerts.telegram_bot import TelegramAlertBot
from execution.risk_manager import InstitutionalRiskManager

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

        self.magic_number = 777111
        self.dry_run = os.getenv("DRY_RUN") == "1"
        self.require_demo_account = bool(self.cfg.get("execution", {}).get("require_demo_account", False))
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
        allowed_assets = self.cfg.get("execution", {}).get("enabled_assets")
        self.execution_assets = set(allowed_assets) if allowed_assets else {
            key for key, value in assets.items() if value.get("enabled", False)
        }
        for asset_key, a_cfg in assets.items():
            if a_cfg.get("enabled", False) and asset_key in self.execution_assets:
                try:
                    self.pipelines[asset_key] = RealtimePipeline(asset_key=asset_key, cfg=self.cfg, data_mode="live")
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

    def _fetch_close_series(self, symbol: str, count: int) -> pd.Series:
        """Загружает последние count M5-свечей и возвращает close."""
        try:
            df = fetch_closed_candles(symbol, "M5", count)
            return df["close"].astype(float).reset_index(drop=True)
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
            s = self._fetch_close_series(symbol, self.corr_history_bars)
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

            # Частичные закрытия. W2: each tranche is quantized to the broker's
            # volume_step/volume_min (so a 0.01 base lot no longer closes the whole
            # position as "TP1 (50%)" or issues a zero-volume TP2 order); a tranche
            # below the minimum is skipped and the remainder stays on the broker TP.
            # W12: tp1_hit/tp2_hit advance only after the partial close is accepted.
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
                        atr_now = 0.0  # would need real ATR fetch; use a rough 1% of price for live safety
                        # In production one would fetch recent ATR; for now use a conservative trail
                        trail_dist = trailing_mult * max(0.0008, (current_price * 0.0006))  # approx ATR
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
                    except Exception:
                        pass
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
            close_msg = (
                f"✅ [{symbol}] TRADE CLOSED #{ticket}\n"
                f"Result: {status_emoji}\n"
                f"Total PnL: ${realized_pnl:+.2f}\n"
                f"Loss streak: {self.streak_losses.get(symbol, 0)}"
            )
            logger.info(close_msg)
            self.bot.send_text_message(close_msg)

        # W10: reflect closed-ticket removal in the persisted management state.
        self._save_management_state()

    def _get_min_dist(self, symbol: str, tick, info) -> float:
        stops_level = info.trade_stops_level * info.point
        freeze_level = info.trade_freeze_level * info.point
        spread = abs(tick.ask - tick.bid)
        return max(stops_level, freeze_level, spread + 30 * info.point)

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

    def _record_execution_result(self, *, asset_key, broker_symbol, action, side,
                                 requested_at, request, result,
                                 position_ticket=None):
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
                metadata={"comment": request.get("comment")},
            )
        except Exception as exc:
            logger.error("Execution ledger write failed: %s", exc)

    def _modify_sl_tp(self, pos, new_sl, new_tp):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "sl": new_sl,
            "tp": new_tp,
        }
        if self.dry_run:
            logger.info(f"[DRY RUN] Would modify SL/TP: {request}")
            return
        res = mt5.order_send(request)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅ Modified SL/TP for #{pos.ticket}: SL={new_sl}, TP={new_tp}")
        else:
            logger.debug(f"Modify failed: {res.comment} ({res.retcode})")

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
        requested_at = now_ms()
        res = mt5.order_send(request)
        asset_key = self.active_trades.get(pos.ticket, {}).get("symbol", pos.symbol)
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
            logger.info(f"✅ Closed {label} for #{pos.ticket} ({volume} lots)")
            return True
        logger.error(f"❌ Partial close failed {label} for #{pos.ticket}: {res.comment} ({res.retcode})")
        return False

    def execute_signal(self, asset_key: str, signal: dict):
        bias = signal["bias"]
        if bias == "no_trade":
            return

        min_conf = self._get_dynamic_min_confidence(asset_key)
        if signal["confidence"] < min_conf:
            logger.info(f"[{asset_key}] Signal suppressed by dynamic threshold: conf={signal['confidence']:.3f} < {min_conf:.3f}")
            return

        # Корреляционный фильтр
        if self._has_correlated_position(asset_key, bias):
            logger.info(f"[{asset_key}] Blocked by correlation filter.")
            return

        can_trade, reason = self.risk_manager.can_trade(asset_key)
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
            for key, default in (("tp1_mult", 1.0), ("tp2_mult", 1.5), ("tp3_mult", 2.0))
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

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_symbol,
            "volume": self.volume,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": f"{asset_key} ML Scalp",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        if self.dry_run:
            logger.info(f"[DRY RUN] Order NOT sent for {asset_key}. Would execute with request above.")
            return

        requested_at = now_ms()
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
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

            self._record_execution_result(
                asset_key=asset_key,
                broker_symbol=mt5_symbol,
                action="open",
                side="buy" if bias == "long" else "sell",
                requested_at=requested_at,
                request=request,
                result=result,
                position_ticket=pos_ticket,
            )
            logger.info(f"🔥 [{asset_key}] ORDER EXECUTED IN MT5! Ticket: #{pos_ticket}, Type: {bias.upper()}, Price: {price}, SL: {sl_price}, TP: {tp_price}")

            try:
                entry_time = int(float(signal.get("timestamp_utc", 0) or 0))
            except (TypeError, ValueError):
                entry_time = int(datetime.now(timezone.utc).timestamp())

            exec_msg = (
                f"🔥 [{asset_key}] ORDER EXECUTED IN MT5!\n"
                f"Ticket: #{pos_ticket}\n"
                f"Type: {bias.upper()}\n"
                f"Price: {price}\n"
                f"SL: {sl_price}\n"
                f"TP1: {targets[0] if targets else 'N/A'}\n"
                f"TP2: {targets[1] if len(targets) > 1 else 'N/A'}\n"
                f"TP3: {raw_tp}\n"
                f"Время: {datetime.now(timezone.utc).isoformat()}"
            )
            self.bot.send_text_message(exec_msg)

            self.active_trades[pos_ticket] = {
                "symbol": asset_key,
                "type": bias,
                "entry_price": price,
                "original_volume": self.volume,
                "tp1": targets[0] if targets else None,
                "tp2": targets[1] if len(targets) > 1 else None,
                "tp3": raw_tp,
                "tp1_hit": False,
                "tp2_hit": False,
            }
            # W10: persist the new position's management state immediately so a
            # restart before the next BE check still knows its TP targets.
            self._save_management_state()

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
                record_position_context(pos_ticket, asset_key, signal)
            except Exception as e:
                logger.error(f"Position context logging failed for #{pos_ticket}: {e}")

            self.risk_manager.record_trade_executed(asset_key)
            self.bot.send_alert_if_qualified(signal, asset_key)
        else:
            self._record_execution_result(
                asset_key=asset_key,
                broker_symbol=mt5_symbol,
                action="open",
                side="buy" if bias == "long" else "sell",
                requested_at=requested_at,
                request=request,
                result=result,
            )
            logger.error(
                f"Order Send Failed for {asset_key}: {result.comment} (Retcode: {result.retcode}), "
                f"request={request}"
            )

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

        while True:
            now = time.time()
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
        shutdown_mt5()
        print("Trader stopped safely.")