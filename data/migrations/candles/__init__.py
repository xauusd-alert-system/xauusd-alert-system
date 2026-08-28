"""Per-table migrations for the ``candles`` table family (ohlcv_m1..h4).

The ``candles`` logical family spans the per-timeframe tables
``ohlcv_m1``, ``ohlcv_m5``, ``ohlcv_m15``, ``ohlcv_h1`` and ``ohlcv_h4``
(data/storage.py). One per-table migration line versions the whole family.
"""
