"""
backtest/engine.py — event-driven backtester for XAU/USD strategy.

Design mandate (from ROADMAP.md Phase 1):
  The backtester drives the *same* RegimeDetector, DonchianBreakoutSignalGenerator,
  and RiskEngine objects the live system uses.  All indicator and sizing logic flows
  through those objects — no parallel code paths.

Bar loop invariant:
  At index i, the strategy sees bars[:i+1] only.  bars[i+1:] is never visible
  to regime/signal/risk at evaluation time.  This is the single most important
  correctness property.

Entry timing:
  Signal fires at close of bar i → entry fills at open of bar i+1 (not same-bar
  close, which would be look-ahead).

Exit timing:
  - Donchian/SMA exit triggers at close of bar i → fill at open of bar i+1.
  - Hard stop hit during bar i → fill at stop (or bar open if gap).

Output schema matches what the dashboard /api/replay endpoint already consumes:
  entry_time, exit_time, entry_price, exit_price, direction, qty,
  slippage_paid, pnl, r_multiple, stop_at_entry
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from core.interfaces import Bar, Regime, Side, SignalType
from core.config import StrategyProfile, SWING
from risk.engine import RiskEngine
from risk.models import OrderRequest, RiskConfig
from strategy.signal_generator import DonchianBreakoutSignalGenerator, RegimeDetector
from backtest.costs import CostModel

logger = logging.getLogger(__name__)

_CONTRACT_VALUE = 100.0   # oz per lot for XAU/USD


@dataclass
class _OpenPosition:
    side:          str       # "LONG" | "SHORT"
    entry_time:    datetime
    entry_price:   float
    qty:           float
    stop_price:    float     # current hard stop (ratcheted by trailing logic)
    stop_at_entry: float     # initial stop — used for R-multiple calc
    atr:           float
    notional:      float = 0.0   # locked-up notional passed to record_position_opened


@dataclass
class _PendingEntry:
    side:           str
    stop_distance:  float
    qty:            float
    atr:            float


@dataclass
class BacktestResult:
    trades:       list[dict] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def expectancy(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t["pnl"] for t in self.trades) / len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t["pnl"] > 0) / len(self.trades)

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak  = self.equity_curve[0][1]
        worst = 0.0
        for _, eq in self.equity_curve:
            peak  = max(peak, eq)
            dd    = (peak - eq) / peak if peak > 0 else 0.0
            worst = max(worst, dd)
        return worst


class BacktestEngine:
    """
    Runs a vectorised-loop backtest over a pre-loaded bar series.

    All indicator and sizing logic is delegated to the live components
    (RegimeDetector, DonchianBreakoutSignalGenerator, RiskEngine), so
    the backtest is provably on the same code path as live trading.
    """

    WARMUP     = 220   # minimum bars before strategy can generate signals
    # Wilder ATR converges in ~3 periods (~42 bars for period=14); SMA200 needs
    # 200 bars; vol_ratio lookback=100 needs another 150 for ATR warm-up.
    # 500 bars is sufficient for all indicators to give production-equivalent values.
    MAX_WINDOW = 500

    def __init__(
        self,
        profile:     StrategyProfile = SWING,
        initial_equity: float        = 100_000.0,
        cost_model:  Optional[CostModel] = None,
    ) -> None:
        self._profile  = profile
        self._eq0      = initial_equity
        self._costs    = cost_model or CostModel()

        # Live components — same objects the orchestrator uses
        self._regime  = RegimeDetector()
        self._signals = DonchianBreakoutSignalGenerator(profile=profile)
        self._risk    = RiskEngine(
            initial_equity = Decimal(str(initial_equity)),
            config         = RiskConfig(
                risk_pct_normal      = Decimal(str(profile.risk_per_trade)),
                daily_loss_limit_pct = Decimal("0.02"),
                weekly_loss_limit_pct= Decimal("0.05"),
                max_drawdown_pct     = Decimal("0.15"),
            ),
        )

    def run(self, bars: list[Bar]) -> BacktestResult:
        """
        Execute the backtest over a full historical bar series.

        bars   : complete history sorted oldest-first
        Returns: BacktestResult with trade list and equity curve
        """
        result = BacktestResult()
        if len(bars) < self.WARMUP + 2:
            logger.warning("Not enough bars for backtest: %d < %d", len(bars), self.WARMUP + 2)
            return result

        open_pos:       Optional[_OpenPosition] = None
        pending_entry:  Optional[_PendingEntry] = None
        pending_exit:   bool                    = False
        _last_bar_date: Optional[date]           = None   # detect session boundaries

        result.equity_curve.append((
            bars[self.WARMUP].timestamp.isoformat(),
            float(self._risk.equity),
        ))

        # Iterate from warmup to len-2 so we always have bars[i+1] available
        for i in range(self.WARMUP, len(bars) - 1):
            bar     = bars[i]
            bar_f   = bars[i + 1]   # "future" bar — entry/exit fills happen at its open
            # Truncate to MAX_WINDOW — Wilder indicators converge well before this
            window  = bars[max(0, i + 1 - self.MAX_WINDOW): i + 1]

            # ── Advance risk engine's session clock to bar date ────────────
            bar_date = bar.timestamp.date()
            if bar_date != _last_bar_date:
                self._risk.on_session_open(self._risk.equity, session_date=bar_date)
                _last_bar_date = bar_date

            # ── OPEN of bar_f: fill pending exits ──────────────────────────
            if pending_exit and open_pos is not None:
                fill_px, slip = self._costs.exit_fill(
                    open_pos.side, float(bar_f.open), open_pos.atr
                )
                pnl = self._calc_pnl(open_pos, fill_px)
                result.trades.append(self._make_trade(
                    open_pos, bar_f.timestamp, fill_px, pnl, slip, "signal_exit"
                ))
                self._risk.record_position_closed(
                    Decimal(str(round(pnl, 2))),
                    long_notional  = Decimal(str(open_pos.notional)) if open_pos.side == "LONG"  else Decimal("0"),
                    short_notional = Decimal(str(open_pos.notional)) if open_pos.side == "SHORT" else Decimal("0"),
                )
                open_pos     = None
                pending_exit = False

            # ── OPEN of bar_f: fill pending entries ────────────────────────
            just_entered = False
            if pending_entry is not None and open_pos is None:
                fill_px, slip = self._costs.entry_fill(
                    pending_entry.side, float(bar_f.open), pending_entry.atr
                )
                stop_px = (
                    fill_px - pending_entry.stop_distance
                    if pending_entry.side == "LONG"
                    else fill_px + pending_entry.stop_distance
                )
                _notional = fill_px * pending_entry.qty * _CONTRACT_VALUE
                open_pos = _OpenPosition(
                    side          = pending_entry.side,
                    entry_time    = bar_f.timestamp,
                    entry_price   = fill_px,
                    qty           = pending_entry.qty,
                    stop_price    = stop_px,
                    stop_at_entry = stop_px,
                    atr           = pending_entry.atr,
                    notional      = _notional,
                )
                self._risk.record_position_opened(
                    long_notional  = Decimal(str(_notional)) if pending_entry.side == "LONG"  else Decimal("0"),
                    short_notional = Decimal(str(_notional)) if pending_entry.side == "SHORT" else Decimal("0"),
                )
                pending_entry = None
                just_entered  = True  # position opens at bar_f; bar[i] data predates the entry

                # Immediately check if entry bar opens past stop (gap)
                hit, fill_stop, slip_stop = self._costs.stop_fill(
                    open_pos.side, open_pos.stop_price,
                    float(bar_f.open), float(bar_f.low), float(bar_f.high), open_pos.atr,
                )
                if hit:
                    pnl = self._calc_pnl(open_pos, fill_stop)
                    result.trades.append(self._make_trade(
                        open_pos, bar_f.timestamp, fill_stop, pnl, slip_stop + slip, "stop_out"
                    ))
                    self._risk.record_position_closed(
                        Decimal(str(round(pnl, 2))),
                        long_notional  = Decimal(str(open_pos.notional)) if open_pos.side == "LONG"  else Decimal("0"),
                        short_notional = Decimal(str(open_pos.notional)) if open_pos.side == "SHORT" else Decimal("0"),
                    )
                    open_pos = None
                    result.equity_curve.append((
                        bar_f.timestamp.isoformat(), float(self._risk.equity)
                    ))
                    continue   # no further logic for this bar

            # ── DURING bar i: check stop on open position ──────────────────
            if open_pos is not None and not just_entered:
                hit, fill_stop, slip_stop = self._costs.stop_fill(
                    open_pos.side, open_pos.stop_price,
                    float(bar.open), float(bar.low), float(bar.high), open_pos.atr,
                )
                if hit:
                    pnl = self._calc_pnl(open_pos, fill_stop)
                    result.trades.append(self._make_trade(
                        open_pos, bar.timestamp, fill_stop, pnl, slip_stop, "stop_out"
                    ))
                    self._risk.record_position_closed(
                        Decimal(str(round(pnl, 2))),
                        long_notional  = Decimal(str(open_pos.notional)) if open_pos.side == "LONG"  else Decimal("0"),
                        short_notional = Decimal(str(open_pos.notional)) if open_pos.side == "SHORT" else Decimal("0"),
                    )
                    open_pos = None

            # ── CLOSE of bar i: strategy evaluation ───────────────────────
            regime = self._regime.detect(window)

            if open_pos is not None:
                # Ratchet trailing stop (Donchian-10, same logic as live)
                open_pos = self._update_trailing_stop(open_pos, window)

                # Check exit conditions
                should_exit, _ = self._signals.should_exit(
                    window, open_pos.side, regime
                )
                if should_exit:
                    pending_exit = True

            # Check entry signal (only when flat and no pending order)
            if open_pos is None and pending_entry is None and not pending_exit:
                signal = self._signals.on_bar(window, regime)
                if signal.signal_type not in (SignalType.NO_SIGNAL, SignalType.EXIT):
                    is_long    = signal.signal_type == SignalType.ENTER_LONG
                    entry_px   = float(bar.close)
                    stop_px    = (
                        entry_px - float(signal.stop_distance)
                        if is_long else
                        entry_px + float(signal.stop_distance)
                    )
                    spread = self._costs.spread_at(bar.timestamp)
                    req = OrderRequest(
                        symbol         = bar.symbol,
                        side           = "LONG" if is_long else "SHORT",
                        entry_price    = Decimal(str(entry_px)),
                        stop_price     = Decimal(str(stop_px)),
                        atr            = signal.atr,
                        current_spread = Decimal(str(round(spread, 4))),
                        median_spread  = Decimal(str(round(self._costs.london_ny_spread, 4))),
                    )
                    decision = self._risk.approve_order(req, bar_date=bar.timestamp.date())
                    if decision.approved:
                        pending_entry = _PendingEntry(
                            side          = "LONG" if is_long else "SHORT",
                            stop_distance = float(signal.stop_distance),
                            qty           = float(decision.quantity),
                            atr           = float(signal.atr),
                        )

            result.equity_curve.append((
                bar.timestamp.isoformat(), float(self._risk.equity)
            ))

        # Force-close any open position at last bar's close
        if open_pos is not None:
            last = bars[-1]
            fill_px, slip = self._costs.exit_fill(
                open_pos.side, float(last.close), open_pos.atr
            )
            pnl = self._calc_pnl(open_pos, fill_px)
            result.trades.append(self._make_trade(
                open_pos, last.timestamp, fill_px, pnl, slip, "end_of_data"
            ))
            self._risk.record_position_closed(
                Decimal(str(round(pnl, 2))),
                long_notional  = Decimal(str(open_pos.notional)) if open_pos.side == "LONG"  else Decimal("0"),
                short_notional = Decimal(str(open_pos.notional)) if open_pos.side == "SHORT" else Decimal("0"),
            )

        logger.info(
            "Backtest complete | bars=%d trades=%d PF=%.2f",
            len(bars), result.n_trades,
            result.profit_factor if result.n_trades else 0,
        )
        return result

    # ── Helpers ──────────────────────────────────────────────────────────

    def _calc_pnl(self, pos: _OpenPosition, fill_price: float) -> float:
        if pos.side == "LONG":
            return (fill_price - pos.entry_price) * pos.qty * _CONTRACT_VALUE
        else:
            return (pos.entry_price - fill_price) * pos.qty * _CONTRACT_VALUE

    def _make_trade(
        self,
        pos:       _OpenPosition,
        exit_time: datetime,
        exit_price: float,
        pnl:       float,
        slip:      float,
        exit_type: str,
    ) -> dict:
        stop_dist = abs(pos.entry_price - pos.stop_at_entry)
        r_per_unit = stop_dist * _CONTRACT_VALUE * pos.qty
        r_multiple = pnl / r_per_unit if r_per_unit > 0 else 0.0
        return {
            "entry_time":    pos.entry_time.isoformat(),
            "exit_time":     exit_time.isoformat(),
            "entry_price":   round(pos.entry_price, 2),
            "exit_price":    round(exit_price, 2),
            "direction":     pos.side,
            "qty":           round(pos.qty, 2),
            "slippage_paid": round(slip, 4),
            "pnl":           round(pnl, 2),
            "r_multiple":    round(r_multiple, 3),
            "stop_at_entry": round(pos.stop_at_entry, 2),
            "exit_type":     exit_type,
        }

    def _update_trailing_stop(
        self,
        pos:    _OpenPosition,
        window: list[Bar],
    ) -> _OpenPosition:
        """Ratchet hard stop to Donchian-10 level — same logic as live RiskEngine."""
        period = 10
        if len(window) < period + 1:
            return pos

        recent = window[-(period + 1):-1]   # completed bars, excluding current
        if pos.side == "LONG":
            d10_low  = min(float(b.low) for b in recent)
            new_stop = max(pos.stop_price, d10_low)
        else:
            d10_high = max(float(b.high) for b in recent)
            new_stop = min(pos.stop_price, d10_high)

        if new_stop == pos.stop_price:
            return pos
        from dataclasses import replace
        return replace(pos, stop_price=new_stop)
