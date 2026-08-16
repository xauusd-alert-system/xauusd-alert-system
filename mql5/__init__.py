"""MQL5 observer sources (SignalDeskObserver) and their wire-contract tests.

This package marker exists so pytest imports ``mql5.tests.test_wire_contract``
instead of colliding with the top-level ``tests`` package; the MQL5 code itself
compiles in MetaEditor on a Windows MT5 host, not here.
"""
