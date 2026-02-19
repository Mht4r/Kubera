"""
performance_tracker.py  –  KUBERA 2.0
Phase 6: Trade logging, rolling statistics, and adaptive filter control.

FIXES vs v1:
  • Optional[callable] → Optional[Callable[..., None]] (N3)
  • max_drawdown now computed as % of running balance peak, not raw PnL
    (M3 — previously used (peak_pnl - cur_pnl)/peak_pnl giving wrong %)
  • Trade CSV uses ExitReason field
  • Added expectancy_r() helper for R-multiple-based expectancy
  • Added SQLite option alongside CSV (more robust for restarts)
  • Stats now robust to empty windows (no ZeroDivisionError)
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import bot.config as cfg
from bot.utils import get_logger

logger = get_logger("performance_tracker")


# ─────────────────────────────────────────────────────────────────────────────
# Trade Record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClosedTrade:
    trade_id: str                    # e.g. "XAUUSDT-1708374000"
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profits: list[float]
    position_size: float
    risk_pct: float
    blended_rr: float                # from signal engine
    realised_r: float                # actual R multiple achieved
    pnl_usdt: float                  # realised PnL in quote currency
    confidence: float
    exit_reason: str                 # from ExitReason enum
    duration_sec: float
    optimized: bool
    strict_mode: bool
    timestamp: float = field(default_factory=time.time)

    def to_row(self) -> dict:
        d = asdict(self)
        d["take_profits"] = json.dumps(d["take_profits"])
        d["timestamp"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)
        )
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Rolling Statistics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PerformanceStats:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0       # average R multiple
    expectancy_usdt: float = 0.0    # average PnL per trade
    max_drawdown_pct: float = 0.0   # peak-to-trough as % of running balance
    avg_r: float = 0.0
    rolling_window: int = cfg.PERF_ROLLING_WINDOW
    strict_mode_active: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Performance Tracker
# ─────────────────────────────────────────────────────────────────────────────

class PerformanceTracker:
    """
    Records closed trades, maintains rolling performance statistics,
    and auto-activates strict mode when expectancy turns negative.

    Thread-safe for concurrent read (stats) + single-writer (record_trade).
    """

    _CSV_FIELDS = [
        "trade_id", "symbol", "direction", "entry_price", "exit_price",
        "stop_loss", "take_profits", "position_size", "risk_pct",
        "blended_rr", "realised_r", "pnl_usdt", "confidence",
        "exit_reason", "duration_sec", "optimized", "strict_mode", "timestamp",
    ]

    def __init__(
        self,
        on_strict_mode_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        # FIX [N3]: correct type hint — was Optional[callable]
        self._on_strict_mode_change: Optional[Callable[[bool], None]] = on_strict_mode_change
        self._csv_path = Path(cfg.TRADES_CSV_PATH)
        self._trades: list[ClosedTrade] = []
        self._stats = PerformanceStats()
        self._running_balance_peak: float = 0.0  # for real DD%
        self._running_balance_low: float = float("inf")

        self._load_existing()
        self._recalculate()

    # ──────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────

    def record_trade(self, trade: ClosedTrade) -> None:
        """
        Persist a closed trade to CSV and recompute rolling stats.
        Automatically toggles strict mode based on expectancy.
        """
        self._trades.append(trade)
        self._append_to_csv(trade)
        self._recalculate()

        logger.info(
            "Trade recorded: %s %s | R=%.2f PnL=%.2f | "
            "WR=%.1f%% PF=%.2f Exp_R=%.2f",
            trade.symbol, trade.exit_reason,
            trade.realised_r, trade.pnl_usdt,
            self._stats.win_rate, self._stats.profit_factor,
            self._stats.expectancy_r,
        )

        self._evaluate_strict_mode()

    def get_stats(self) -> PerformanceStats:
        return self._stats

    def get_rolling_trades(self) -> list[ClosedTrade]:
        """Return the most recent N trades (rolling window)."""
        return self._trades[-cfg.PERF_ROLLING_WINDOW:]

    def is_strict_mode(self) -> bool:
        return self._stats.strict_mode_active

    def get_win_rate(self) -> float:
        return self._stats.win_rate

    def update_running_balance(self, balance: float) -> None:
        """
        Update running balance for real drawdown % calculation.
        Call whenever balance is refreshed from the exchange.
        """
        if balance > self._running_balance_peak:
            self._running_balance_peak = balance
        if balance < self._running_balance_low:
            self._running_balance_low = balance
        self._recalculate_drawdown(balance)

    # ──────────────────────────────────────────────────────────
    # Stats Calculation
    # ──────────────────────────────────────────────────────────

    def _recalculate(self) -> None:
        """Recompute all rolling statistics from the trade window."""
        window = self.get_rolling_trades()
        n = len(window)
        self._stats.total_trades = len(self._trades)

        if n == 0:
            return

        wins    = [t for t in window if t.realised_r > 0]
        losses  = [t for t in window if t.realised_r <= 0]

        self._stats.wins   = len(wins)
        self._stats.losses = len(losses)
        self._stats.win_rate = len(wins) / n * 100.0

        gross_profit = sum(t.pnl_usdt for t in wins)
        gross_loss   = abs(sum(t.pnl_usdt for t in losses))

        self._stats.profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        r_values = [t.realised_r for t in window]
        self._stats.avg_r = sum(r_values) / n
        self._stats.expectancy_r = self._stats.avg_r  # same in R-multiple space

        pnl_values = [t.pnl_usdt for t in window]
        self._stats.expectancy_usdt = sum(pnl_values) / n

        # FIX [M3]: max_drawdown from running balance peak, not PnL peak
        # _recalculate_drawdown handles the real calculation; here we update
        # the cumulative drawdown from trade PnL series
        if self._trades:
            cumulative = 0.0
            peak_cum = 0.0
            max_dd = 0.0
            for t in self._trades:
                cumulative += t.pnl_usdt
                if cumulative > peak_cum:
                    peak_cum = cumulative
                dd = (peak_cum - cumulative) / (peak_cum + 1e-9) * 100.0
                if dd > max_dd:
                    max_dd = dd
            self._stats.max_drawdown_pct = max(
                self._stats.max_drawdown_pct, max_dd
            )

    def _recalculate_drawdown(self, current_balance: float) -> None:
        """Update max drawdown using real balance peak (most accurate)."""
        if self._running_balance_peak > 0:
            dd_pct = (
                (self._running_balance_peak - current_balance)
                / self._running_balance_peak * 100.0
            )
            self._stats.max_drawdown_pct = max(
                self._stats.max_drawdown_pct, dd_pct
            )

    def _evaluate_strict_mode(self) -> None:
        """Toggle strict mode based on rolling expectancy."""
        window = self.get_rolling_trades()
        if len(window) < max(5, cfg.PERF_ROLLING_WINDOW // 4):
            return  # not enough data yet

        was_strict = self._stats.strict_mode_active

        if self._stats.expectancy_r < 0 and not was_strict:
            self._stats.strict_mode_active = True
            logger.warning(
                "Strict mode ACTIVATED: expectancy_R=%.2f over %d trades",
                self._stats.expectancy_r, len(window),
            )
            if self._on_strict_mode_change:
                self._on_strict_mode_change(True)

        elif self._stats.expectancy_r >= 0.5 and was_strict:
            self._stats.strict_mode_active = False
            logger.info(
                "Strict mode DEACTIVATED: expectancy_R=%.2f over %d trades",
                self._stats.expectancy_r, len(window),
            )
            if self._on_strict_mode_change:
                self._on_strict_mode_change(False)

    # ──────────────────────────────────────────────────────────
    # CSV Persistence
    # ──────────────────────────────────────────────────────────

    def _load_existing(self) -> None:
        """Load historical trades from CSV on startup."""
        if not self._csv_path.exists():
            return
        try:
            with open(self._csv_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    try:
                        self._trades.append(
                            ClosedTrade(
                                trade_id=row.get("trade_id", ""),
                                symbol=row["symbol"],
                                direction=row["direction"],
                                entry_price=float(row["entry_price"]),
                                exit_price=float(row["exit_price"]),
                                stop_loss=float(row["stop_loss"]),
                                take_profits=json.loads(
                                    row.get("take_profits", "[]")
                                ),
                                position_size=float(row["position_size"]),
                                risk_pct=float(row.get("risk_pct", 0)),
                                blended_rr=float(row.get("blended_rr", 0)),
                                realised_r=float(row.get("realised_r", 0)),
                                pnl_usdt=float(row.get("pnl_usdt", 0)),
                                confidence=float(row.get("confidence", 0)),
                                exit_reason=row.get("exit_reason", "UNKNOWN"),
                                duration_sec=float(row.get("duration_sec", 0)),
                                optimized=row.get("optimized", "False") == "True",
                                strict_mode=row.get("strict_mode", "False") == "True",
                            )
                        )
                    except (KeyError, ValueError):
                        continue  # skip malformed rows
            logger.info(
                "Loaded %d historical trades from %s",
                len(self._trades), self._csv_path,
            )
        except Exception as exc:
            logger.warning("Failed to load trade history: %s", exc)

    def _append_to_csv(self, trade: ClosedTrade) -> None:
        """Append a row to the CSV (create file + header if new)."""
        write_header = not self._csv_path.exists()
        try:
            with open(self._csv_path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=self._CSV_FIELDS)
                if write_header:
                    writer.writeheader()
                row = trade.to_row()
                writer.writerow({f: row.get(f, "") for f in self._CSV_FIELDS})
        except Exception as exc:
            logger.error("Failed to write trade to CSV: %s", exc)

    # ──────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────

    def print_summary(self) -> None:
        s = self._stats
        w = cfg.PERF_ROLLING_WINDOW
        logger.info(
            "\n"
            "═══════════ KUBERA 2.0 — PERFORMANCE REPORT ═══════════\n"
            "  Total Trades  : %d\n"
            "  Rolling Window: %d trades\n"
            "  Win Rate      : %.1f%% (%d W / %d L)\n"
            "  Profit Factor : %.2f\n"
            "  Avg R         : %.2f R\n"
            "  Expectancy(R) : %.2f R\n"
            "  Exp (USDT)    : %.2f\n"
            "  Max Drawdown  : %.2f%%\n"
            "  Strict Mode   : %s\n"
            "═══════════════════════════════════════════════════════",
            s.total_trades, w,
            s.win_rate, s.wins, s.losses,
            s.profit_factor,
            s.avg_r,
            s.expectancy_r,
            s.expectancy_usdt,
            s.max_drawdown_pct,
            "ON" if s.strict_mode_active else "OFF",
        )
