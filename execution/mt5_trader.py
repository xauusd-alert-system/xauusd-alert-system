"""
Live Multi-Asset MT5 Auto-Trader with Full Telegram Live Notifications.
Sends Entry Signals, TP1 Breakeven Alerts, and Final Close PnL Reports.
Includes Automatic Stops-Level & Digits Adjuster for BTCUSD and Altcoins.
"""
import os
import sys
import time
import logging
from datetime import datetime, timezone
import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, get_env, get_signal_grid
from data.mt5_provider import initialize_mt5, shutdown_mt5, validate_symbol, fetch_closed_candles
from data.trade_logger import init_trade_log_schema, log_trade_entry, log_trade_close
from realtime.pipeline import RealtimePipeline
from alerts.telegram_bot import TelegramAlertBot
from execution.risk_manager import InstitutionalRiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("multi_asset_trader")


class MultiAssetMT5Trader:
    def __init__(self):
        self.cfg = load_config()
        self.risk_manager = InstitutionalRiskManager(self.cfg)
        self.bot = TelegramAlertBot(self.cfg)

        self.magic_number = 777111
        self.volume = 0.01
        self.dry_run = os.getenv("DRY_RUN") == "1"

        self.pipelines = {}
        assets = self.cfg.get("assets", {})
        for asset_key, a_cfg in assets.items():
            if a_cfg.get("enabled", False):
                try:
                    self.pipelines[asset_key] = RealtimePipeline(asset_key=asset_key, cfg=self.cfg, data_mode="live")
                    logger.info(f"Loaded pipeline for {asset_key}")
                except Exception as e:
                    logger.warning(f"Could not load pipeline for {asset_key}: {e}")

        self.be_state = {}
        self.active_trades = {}
        self.streak_losses = {}

        # Early-breakeven trigger per MT5 symbol (signal_grid.breakeven_trigger_atr).
        # < 1.0 moves the stop to entry before TP1 (protects mean-reverting FX from
        # the 3x-step loss tail); 1.0 = legacy (BE only at TP1).
        self.be_trigger_by_symbol = {}
        self.trailing_atr_mult_by_symbol = {}
        for asset_key, a_cfg in assets.items():
            sym = a_cfg.get("mt5_symbol")
            if sym:
                grid = get_signal_grid(self.cfg, a_cfg)
                self.be_trigger_by_symbol[sym] = float(grid.get("breakeven_trigger_atr", 1.0))
                # v4b trailing (None = legacy)
                self.trailing_atr_mult_by_symbol[sym] = grid.get("trailing_atr_mult")

        # CRIT 5: path to the executed-trades SQLite DB (the same file used by
        # scripts/retrain_with_real_trades.py). Overridable via env for tests.
        # get_env may return None; coerce to str so schema init/log calls type-check.
        self.trade_db_path = str(get_env("TRADE_LOG_DB_PATH", default=self.cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")))
        init_trade_log_schema(self.trade_db_path)
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
        positions = mt5.positions_get(magic=self.magic_number)
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
        positions = mt5.positions_get(magic=self.magic_number)
        current_tickets = set()
        logger.info(f"=== BE CHECK: found {len(positions) if positions else 0} positions (magic={self.magic_number}) ===")

        if not positions:
            self.active_trades = {}
            return

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

            # Частичные закрытия (без изменений, как в предыдущей версии)
            if pos.type == 0:
                if tp1 is not None and not tp1_hit and current_price >= tp1:
                    close_vol = round(original_volume * 0.5, 2)
                    if pos.volume >= close_vol:
                        self._close_partial_position(pos, tick.bid, close_vol, "TP1 (50%)")
                        trade_data["tp1_hit"] = True
                        target_sl = round(pos.price_open + (10 ** -digits), digits)
                        min_sl = current_price - self._get_min_dist(symbol, tick, info)
                        if target_sl < min_sl:
                            target_sl = min_sl
                        self._modify_sl_tp(pos, target_sl, pos.tp)
                elif tp2 is not None and tp1_hit and not tp2_hit and current_price >= tp2:
                    close_vol = round(original_volume * 0.3, 2)
                    if pos.volume >= close_vol:
                        self._close_partial_position(pos, tick.bid, close_vol, "TP2 (30%)")
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
                    close_vol = round(original_volume * 0.5, 2)
                    if pos.volume >= close_vol:
                        self._close_partial_position(pos, tick.ask, close_vol, "TP1 (50%)")
                        trade_data["tp1_hit"] = True
                        target_sl = round(pos.price_open - (10 ** -digits), digits)
                        min_sl = current_price + self._get_min_dist(symbol, tick, info)
                        if target_sl > min_sl:
                            target_sl = min_sl
                        self._modify_sl_tp(pos, target_sl, pos.tp)
                elif tp2 is not None and tp1_hit and not tp2_hit and current_price <= tp2:
                    close_vol = round(original_volume * 0.3, 2)
                    if pos.volume >= close_vol:
                        self._close_partial_position(pos, tick.ask, close_vol, "TP2 (30%)")
                        trade_data["tp2_hit"] = True
                elif tp3 is not None and tp2_hit and current_price <= tp3:
                    self._close_partial_position(pos, tick.ask, pos.volume, "TP3 (20%)")

        # Детектор закрытия
        closed_tickets = set(self.active_trades.keys()) - current_tickets
        for ticket in closed_tickets:
            trade_info = self.active_trades.pop(ticket, {})
            symbol = trade_info.get("symbol", "ASSET")
            history_deals = mt5.history_deals_get(position=ticket)
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

    def _get_min_dist(self, symbol: str, tick, info) -> float:
        stops_level = info.trade_stops_level * info.point
        freeze_level = info.trade_freeze_level * info.point
        spread = abs(tick.ask - tick.bid)
        return max(stops_level, freeze_level, spread + 30 * info.point)

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
            return
        res = mt5.order_send(request)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅ Closed {label} for #{pos.ticket} ({volume} lots)")
        else:
            logger.error(f"❌ Partial close failed {label} for #{pos.ticket}: {res.comment} ({res.retcode})")

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

        positions = mt5.positions_get(symbol=mt5_symbol, magic=self.magic_number)
        if positions:
            return

        tick = mt5.symbol_info_tick(mt5_symbol)
        info = mt5.symbol_info(mt5_symbol)
        if not tick or not info:
            return

        order_type = mt5.ORDER_TYPE_BUY if bias == "long" else mt5.ORDER_TYPE_SELL
        price = tick.ask if bias == "long" else tick.bid

        invalidation = float(signal["invalidation"])
        targets = signal["targets"]
        raw_tp = float(targets[2] if len(targets) > 2 else targets[-1])

        try:
            sl_price, tp_price = self._normalize_stops(mt5_symbol, bias, price, invalidation, raw_tp)
        except Exception as e:
            logger.error(f"Normalization failed for {mt5_symbol}: {e}")
            return

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
                opened = mt5.positions_get(symbol=mt5_symbol, magic=self.magic_number)
                if opened:
                    pos_ticket = int(opened[-1].ticket)
            except Exception as e:  # pragma: no cover - defensive fallback
                logger.warning(f"[{asset_key}] Could not resolve position ticket, using order ticket {pos_ticket}: {e}")

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

            self.risk_manager.record_trade_executed(asset_key)
            self.bot.send_alert_if_qualified(signal, asset_key)
        else:
            logger.error(
                f"Order Send Failed for {asset_key}: {result.comment} (Retcode: {result.retcode}), "
                f"request={request}"
            )

    def run_loop(self):
        initialize_mt5()
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

            current_bar_time = 0
            for asset_key, a_cfg in self.cfg["assets"].items():
                if not a_cfg.get("enabled", False):
                    continue
                symbol = a_cfg["mt5_symbol"]
                try:
                    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 1, 1)
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
                logger.info(f"New M5 bar detected: {current_bar_time} (last: {last_bar_time})")
                last_bar_time = current_bar_time
                logger.info("--- Analyzing newly closed M5 candle across all assets ---")
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
    try:
        trader.run_loop()
    except KeyboardInterrupt:
        shutdown_mt5()
        print("Trader stopped safely.")