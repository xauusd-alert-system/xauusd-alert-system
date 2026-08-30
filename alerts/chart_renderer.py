"""
Visual Chart Renderer for Telegram Alerts & Dashboard Previews.
Generates clean SVG and ASCII chart snapshots with Entry, Stop Loss,
and Take Profit target levels.
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd


class ChartRenderer:
    """
    Renders visual candlestick and level charts.
    """

    @staticmethod
    def render_svg_candlestick(
        df: pd.DataFrame,
        symbol: str,
        entry_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_prices: Optional[List[float]] = None,
        width: int = 600,
        height: int = 300,
    ) -> str:
        """
        Generates standalone SVG markup of candlesticks with overlay price levels.
        """
        if df.empty or len(df) < 2:
            return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#94a3b8" text-anchor="middle">No chart data</text></svg>'

        n = min(len(df), 40)
        slice_df = df.tail(n).reset_index(drop=True)

        min_p = slice_df["low"].min()
        max_p = slice_df["high"].max()

        if entry_price:
            min_p = min(min_p, entry_price)
            max_p = max(max_p, entry_price)
        if sl_price:
            min_p = min(min_p, sl_price)
            max_p = max(max_p, sl_price)
        if tp_prices:
            for tp in tp_prices:
                min_p = min(min_p, tp)
                max_p = max(max_p, tp)

        p_range = max(max_p - min_p, 1e-6)
        pad_top = 30
        pad_bot = 30
        plot_h = height - pad_top - pad_bot

        def scale_y(price: float) -> float:
            return pad_top + plot_h * (1.0 - (price - min_p) / p_range)

        candle_w = max((width - 80) / n, 4)

        svg = [
            f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="background:#0f172a; font-family:monospace;">',
            '<rect width="100%" height="100%" fill="#0f172a"/>',
            f'<text x="20" y="22" fill="#f8fafc" font-size="14" font-weight="bold">{symbol} M5 Setup</text>',
        ]

        # Draw grid
        for i in range(4):
            grid_p = min_p + p_range * (i / 3.0)
            gy = scale_y(grid_p)
            svg.append(f'<line x1="20" y1="{gy}" x2="{width - 20}" y2="{gy}" stroke="#334155" stroke-dasharray="3,3"/>')
            svg.append(f'<text x="{width - 60}" y="{gy - 4}" fill="#64748b" font-size="10">{grid_p:.2f}</text>')

        # Draw candles
        for idx, row in slice_df.iterrows():
            cx = 30 + idx * candle_w + candle_w / 2.0
            o_y = scale_y(row["open"])
            c_y = scale_y(row["close"])
            h_y = scale_y(row["high"])
            l_y = scale_y(row["low"])

            is_green = row["close"] >= row["open"]
            color = "#10b981" if is_green else "#f43f5e"

            # Wick
            svg.append(f'<line x1="{cx}" y1="{h_y}" x2="{cx}" y2="{l_y}" stroke="{color}" stroke-width="1.5"/>')
            # Body
            top_b = min(o_y, c_y)
            bot_b = max(o_y, c_y)
            body_h = max(bot_b - top_b, 1.5)
            svg.append(
                f'<rect x="{cx - candle_w * 0.35}" y="{top_b}" width="{candle_w * 0.7}" height="{body_h}" fill="{color}"/>'
            )

        # Draw Trade Levels
        if entry_price:
            ey = scale_y(entry_price)
            svg.append(
                f'<line x1="20" y1="{ey}" x2="{width - 20}" y2="{ey}" stroke="#38bdf8" stroke-width="2" stroke-dasharray="4,4"/>'
            )
            svg.append(
                f'<text x="25" y="{ey - 5}" fill="#38bdf8" font-size="11" font-weight="bold">ENTRY: {entry_price:.2f}</text>'
            )

        if sl_price:
            sy = scale_y(sl_price)
            svg.append(f'<line x1="20" y1="{sy}" x2="{width - 20}" y2="{sy}" stroke="#f43f5e" stroke-width="2"/>')
            svg.append(
                f'<text x="25" y="{sy - 5}" fill="#f43f5e" font-size="11" font-weight="bold">STOP: {sl_price:.2f}</text>'
            )

        if tp_prices:
            for i, tp in enumerate(tp_prices):
                ty = scale_y(tp)
                svg.append(
                    f'<line x1="20" y1="{ty}" x2="{width - 20}" y2="{ty}" stroke="#10b981" stroke-width="1.5" stroke-dasharray="2,2"/>'
                )
                svg.append(
                    f'<text x="{width - 120}" y="{ty - 5}" fill="#10b981" font-size="11" font-weight="bold">TP{i + 1}: {tp:.2f}</text>'
                )

        svg.append("</svg>")
        return "\n".join(svg)

    @staticmethod
    def render_ascii_levels(
        symbol: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
        direction: str = "LONG",
    ) -> str:
        """
        Renders a lightweight ASCII level map suitable for Telegram messages.
        """
        arrow = "🟢 ▲" if direction.upper() == "LONG" else "🔴 ▼"
        lines = [
            f"📊 *{symbol} Setup Map ({arrow} {direction.upper()})*",
            "```",
            f" 🎯 TP3 : {tp3:>10.2f}  (20% Final)",
            f" 🎯 TP2 : {tp2:>10.2f}  (30% Runner)",
            f" 🎯 TP1 : {tp1:>10.2f}  (50% + Breakeven)",
            f" ─── ENTRY ─── {entry:>8.2f}",
            f" 🛑 STOP: {sl:>10.2f}  (ATR Risk)",
            "```",
        ]
        return "\n".join(lines)
