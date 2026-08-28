"""
Tests for Visual Chart Renderer.
"""

import pandas as pd

from alerts.chart_renderer import ChartRenderer


def test_svg_chart_generation():
    df = pd.DataFrame(
        {
            "open": [2000.0, 2005.0, 2003.0, 2008.0],
            "high": [2006.0, 2009.0, 2007.0, 2012.0],
            "low": [1998.0, 2002.0, 2001.0, 2006.0],
            "close": [2004.0, 2003.0, 2007.0, 2011.0],
        }
    )
    svg = ChartRenderer.render_svg_candlestick(
        df,
        symbol="XAUUSD",
        entry_price=2010.0,
        sl_price=2000.0,
        tp_prices=[2015.0, 2020.0, 2025.0],
    )
    assert "<svg" in svg
    assert "</svg>" in svg
    assert "ENTRY: 2010.00" in svg
    assert "STOP: 2000.00" in svg
    assert "TP1: 2015.00" in svg


def test_ascii_levels_generation():
    ascii_map = ChartRenderer.render_ascii_levels(
        symbol="XAUUSD",
        entry=2500.0,
        sl=2490.0,
        tp1=2510.0,
        tp2=2518.0,
        tp3=2528.0,
        direction="LONG",
    )
    assert "XAUUSD Setup Map" in ascii_map
    assert "TP1" in ascii_map
    assert "ENTRY" in ascii_map
    assert "STOP" in ascii_map
