"""
Live order-book (Depth-of-Market) feed for MT5 symbols that expose a book.

Empirics on FxPro demo (2026-08-18): only BITCOIN delivers a real book via
market_book_add / market_book_get (10 levels per side, real volumes). XAUUSD /
XAGUSD reject the subscription outright and EURUSD / GBPUSD subscribe but stay
empty. The feed is therefore per-asset opt-in
(``config.book_gate.assets.<KEY>.enabled``) and every consumer must be
fail-open: an asset without DOM simply has no book features (signal unchanged).

The poller runs in its own daemon thread so it never blocks the trader's main
loop. Per M5-bar aggregates are appended to ``data/book_bars/<MT5_SYMBOL>.csv``
(persist=True, the trader process) so Phase 2 can validate / retrain on real
book history; the pipeline gate reads the last finalized bar's features via
``bar_features()``. ``bar_features`` finalizes the bucket of a closed bar
on demand (closing the poll-race at the M5 boundary), so by signal time the
just-closed bar is always available when the feed is healthy.
"""

import csv
import logging
import math
import os
import threading
import time

logger = logging.getLogger("book_feed")

BAR_SECONDS = 300  # M5 setup timeframe, matching config market_data.timeframe

CSV_FIELDS = [
    "bar_utc",
    "snapshots",
    "imb1_last",
    "imb3_last",
    "imb5_last",
    "imb_all_last",
    "imb5_mean",
    "imb5_std",
    "depth_ratio_last",
    "depth_ratio_mean",
    "walls_max",
    "spread_mean",
    "microprice_last",
    "flow_sum",
]


def bar_ts_of(epoch_s: int, bar_seconds: int = BAR_SECONDS) -> int:
    """Bar-start epoch seconds for an M5 boundary."""
    return int(epoch_s // bar_seconds) * bar_seconds


def book_features_from_levels(bids, asks) -> dict | None:
    """Per-snapshot book features.

    bids/asks: iterables of (price, volume) ordered by proximity to the mid
    (bids descending, asks ascending). Returns None when either side is empty.

    Imbalance convention: +1.0 = ask-heavy (sell pressure).
    """
    bids = list(bids)
    asks = list(asks)
    if not bids or not asks:
        return None

    def imb(bid_vol: float, ask_vol: float) -> float:
        total = bid_vol + ask_vol
        return 0.0 if total <= 0.0 else (ask_vol - bid_vol) / total

    def cum_vol(levels, n) -> float:
        return sum(v for _, v in levels[:n])

    b1, a1 = bids[0][1], asks[0][1]
    b3, a3 = cum_vol(bids, 3), cum_vol(asks, 3)
    b5, a5 = cum_vol(bids, 5), cum_vol(asks, 5)
    full_b = sum(v for _, v in bids)
    full_a = sum(v for _, v in asks)

    vols = [v for _, v in bids] + [v for _, v in asks]
    vols_sorted = sorted(vols)
    median = vols_sorted[len(vols_sorted) // 2] if vols_sorted else 0.0
    walls = sum(1 for v in vols if median > 0.0 and v >= 10.0 * median)

    total_vol = full_b + full_a
    microprice = (sum(p * v for p, v in bids) + sum(p * v for p, v in asks)) / total_vol if total_vol > 0.0 else 0.0

    return {
        "imb1": round(imb(b1, a1), 4),
        "imb3": round(imb(b3, a3), 4),
        "imb5": round(imb(b5, a5), 4),
        "imb_all": round(imb(full_b, full_a), 4),
        "depth_ratio": round(full_b / full_a, 4) if full_a > 0.0 else 0.0,
        "walls": walls,
        "spread": round(asks[0][0] - bids[0][0], 6),
        "microprice": round(microprice, 6),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
    }


class BookFeed:
    """Per-asset DOM subscription, snapshot poller and per-bar aggregation."""

    def __init__(self, cfg: dict, persist: bool = True, out_dir: str = None):
        bg = cfg.get("book_gate") or {}
        self.enabled = bool(bg.get("enabled", True))
        self.poll_interval_s = float(bg.get("poll_interval_s", 3.0))
        self.persist = persist
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.out_dir = out_dir or os.path.join(root, "data", "book_bars")

        self.assets = {}
        for key, acfg in (cfg.get("assets") or {}).items():
            abg = (bg.get("assets") or {}).get(key) or {}
            self.assets[key] = {
                "mt5_symbol": acfg.get("mt5_symbol"),
                "enabled": bool(abg.get("enabled", False)) and bool(acfg.get("enabled", False)),
            }

        self._bars = {}  # asset_key -> {bar_ts: aggregated features}
        self._current = {}  # asset_key -> accumulating bucket or None
        self._status = {}  # asset_key -> diagnostics
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._mt5 = None

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        if not self.enabled or not any(a["enabled"] for a in self.assets.values()):
            logger.info("[BOOK] feed disabled (no book_gate.assets enabled)")
            return
        self._thread = threading.Thread(target=self._run, name="book-feed", daemon=True)
        self._thread.start()
        logger.info("[BOOK] feed started (poll=%ss, persist=%s)", self.poll_interval_s, self.persist)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self):
        from mt5_adapter.lazy import get_mt5_module

        # ТЗ 8.6: raw module handle via the adapter.
        self._mt5 = mt5 = get_mt5_module()
        self._subscribe_all()
        while not self._stop.wait(self.poll_interval_s):
            try:
                self._poll_once()
            except Exception as exc:  # never kill the feed
                logger.error("[BOOK] poll loop error: %s", exc)
        self._unsubscribe_all()
        logger.info("[BOOK] feed stopped")

    def _subscribe_all(self):
        mt5 = self._mt5
        for key, acfg in self.assets.items():
            if not acfg["enabled"]:
                continue
            add_ok = mt5.market_book_add(acfg["mt5_symbol"])
            # The add() flag is advisory: when another connection already holds
            # the DOM subscription the terminal may answer False while
            # market_book_get still streams data. Probe one read and treat the
            # feed as subscribed when data actually flows.
            probe_ok = False
            try:
                probe = mt5.market_book_get(acfg["mt5_symbol"])
                probe_ok = bool(probe) and len(list(probe)) > 0
            except Exception:
                probe_ok = False
            subscribed = bool(add_ok) or probe_ok
            with self._lock:
                self._status[key] = {
                    "subscribed": subscribed,
                    "add_rc": bool(add_ok),
                    "snapshot_count": 0,
                    "last_snapshot_ts": None,
                    "last_snapshot_ok": False,
                    "last_bar_ts": None,
                    "error": None,
                }
            logger.info(
                "[BOOK] %s (%s): subscribed=%s (add_ok=%s, probe_ok=%s)",
                key,
                acfg["mt5_symbol"],
                subscribed,
                add_ok,
                probe_ok,
            )

    def _unsubscribe_all(self):
        for key, acfg in self.assets.items():
            if acfg["enabled"]:
                try:
                    self._mt5.market_book_remove(acfg["mt5_symbol"])
                except Exception:
                    pass

    # ------------------------------------------------------------------ polling

    def _poll_once(self):
        now = int(time.time())
        bar = bar_ts_of(now)
        for key, acfg in self.assets.items():
            if not acfg["enabled"]:
                continue
            try:
                feats = self._snapshot_features(acfg["mt5_symbol"])
                with self._lock:
                    status = self._status.get(key, {})
                    status["last_snapshot_ts"] = now
                    status["error"] = None
                    if feats is None:
                        status["last_snapshot_ok"] = False
                    else:
                        status["last_snapshot_ok"] = True
                        status["snapshot_count"] = int(status.get("snapshot_count", 0)) + 1
                        if not status.get("subscribed"):
                            # add() can answer False while data still flows
                            # (terminal-side DOM state); receiving snapshots is
                            # the truthful definition of "subscribed".
                            status["subscribed"] = True
                            logger.info("[BOOK] %s: receiving snapshots -> subscribed=True", key)
                        self._accumulate(key, bar, feats)
            except Exception as exc:
                logger.warning("[BOOK] %s snapshot failed: %s", key, exc)
                with self._lock:
                    self._status.setdefault(key, {}).update({"error": str(exc), "last_snapshot_ts": now})

    def _snapshot_features(self, mt5_symbol: str) -> dict | None:
        mt5 = self._mt5
        book = mt5.market_book_get(mt5_symbol)
        tick = mt5.symbol_info_tick(mt5_symbol)
        if not book or not tick:
            return None
        mid = (float(tick.bid) + float(tick.ask)) / 2.0
        bids = []
        asks = []
        for level in book:
            price = float(getattr(level, "price", 0.0))
            volume = float(getattr(level, "volume", 0.0) or 0.0)
            if price <= 0.0 or volume <= 0.0:
                continue
            if price <= mid:
                bids.append((price, volume))
            else:
                asks.append((price, volume))
        bids.sort(key=lambda x: -x[0])  # nearest bid first
        asks.sort(key=lambda x: x[0])  # nearest ask first
        return book_features_from_levels(bids, asks)

    # ------------------------------------------------------------------ aggregation

    def _accumulate(self, key: str, bar: int, feats: dict):
        cur = self._current.get(key)
        if cur is None or cur["bar"] != bar:
            if cur is not None:
                self._finalize(key, cur)
            cur = {
                "bar": bar,
                "n": 0,
                "imb5_sum": 0.0,
                "imb5_sq": 0.0,
                "imb1_last": 0.0,
                "imb3_last": 0.0,
                "imb5_last": 0.0,
                "imb_all_last": 0.0,
                "depth_ratio_sum": 0.0,
                "depth_ratio_last": 0.0,
                "walls_max": 0,
                "spread_sum": 0.0,
                "microprice_last": 0.0,
                "flow_sum": 0.0,
                "prev_imb5": None,
            }
            self._current[key] = cur
        cur["n"] += 1
        cur["imb5_sum"] += feats["imb5"]
        cur["imb5_sq"] += feats["imb5"] ** 2
        cur["imb1_last"] = feats["imb1"]
        cur["imb3_last"] = feats["imb3"]
        cur["imb5_last"] = feats["imb5"]
        cur["imb_all_last"] = feats["imb_all"]
        cur["depth_ratio_sum"] += feats["depth_ratio"]
        cur["depth_ratio_last"] = feats["depth_ratio"]
        cur["walls_max"] = max(cur["walls_max"], int(feats["walls"]))
        cur["spread_sum"] += feats["spread"]
        cur["microprice_last"] = feats["microprice"]
        if cur["prev_imb5"] is not None:
            cur["flow_sum"] += feats["imb5"] - cur["prev_imb5"]
        cur["prev_imb5"] = feats["imb5"]
        self._status.setdefault(key, {})["last_bar_ts"] = bar

    def _finalize(self, key: str, cur: dict) -> dict:
        n = max(cur["n"], 1)
        mean5 = cur["imb5_sum"] / n
        agg = {
            "bar_utc": cur["bar"],
            "snapshots": cur["n"],
            "imb1_last": cur["imb1_last"],
            "imb3_last": cur["imb3_last"],
            "imb5_last": cur["imb5_last"],
            "imb_all_last": cur["imb_all_last"],
            "imb5_mean": round(mean5, 4),
            "imb5_std": round(math.sqrt(max(0.0, cur["imb5_sq"] / n - mean5**2)), 4),
            "depth_ratio_last": cur["depth_ratio_last"],
            "depth_ratio_mean": round(cur["depth_ratio_sum"] / n, 4),
            "walls_max": cur["walls_max"],
            "spread_mean": round(cur["spread_sum"] / n, 6),
            "microprice_last": cur["microprice_last"],
            "flow_sum": round(cur["flow_sum"], 4),
        }
        bars = self._bars.setdefault(key, {})
        bars[cur["bar"]] = agg
        # keep memory bounded: last 48 bars (4 hours)
        for stale in [b for b in bars if b < cur["bar"] - BAR_SECONDS * 47]:
            del bars[stale]
        if self.persist:
            try:
                self._append_csv(key, agg)
            except Exception as exc:
                logger.error("[BOOK] %s csv append failed: %s", key, exc)
        return agg

    def _append_csv(self, key: str, agg: dict):
        symbol = self.assets[key]["mt5_symbol"]
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"{symbol}.csv")
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow({f: agg.get(f) for f in CSV_FIELDS})

    # ------------------------------------------------------------------ accessors

    def bar_features(self, asset_key: str, bar_ts: int) -> dict | None:
        """Features of a CLOSED bar. Finalizes the bucket on demand so the
        just-closed bar is available at signal time (causal: the bar has
        already ended; no further snapshots can arrive for it)."""
        with self._lock:
            bars = self._bars.get(asset_key, {})
            if bar_ts in bars:
                return bars[bar_ts]
            cur = self._current.get(asset_key)
            if cur is not None and cur["bar"] == bar_ts and cur["n"] > 0:
                agg = self._finalize(asset_key, cur)
                self._current[asset_key] = None
                return agg
            return None

    def overview(self) -> dict:
        """JSON-safe status for the dashboard (one entry per configured asset)."""
        with self._lock:
            out = {}
            for key, acfg in self.assets.items():
                status = {
                    "subscribed": False,
                    "snapshot_count": 0,
                    "last_snapshot_ts": None,
                    "last_snapshot_ok": False,
                    "last_bar_ts": None,
                    "error": None,
                    **dict(self._status.get(key, {})),
                }
                status["configured"] = acfg["enabled"]
                # Show the most recent FINALIZED bar's features (the current
                # forming bar has no finalized aggregates yet).
                bars = self._bars.get(key, {})
                if bars:
                    latest_bar = max(bars)
                    status["last_bar_ts"] = latest_bar
                    status["last_bar_features"] = bars[latest_bar]
                out[key] = status
            return out
