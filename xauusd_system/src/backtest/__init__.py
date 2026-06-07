"""
backtest — event-driven backtester that reuses the live strategy path.

Entry point:
    python -m backtest.run --profile swing --start 2023-01-01 --end 2026-01-01

The backtester drives the same RegimeDetector, DonchianBreakoutSignalGenerator,
and RiskEngine objects the live system uses.  Parity with the live path is
enforced by the test suite (tests/test_backtest_parity.py).
"""
