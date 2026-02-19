"""
risk_engine.py  –  KUBERA 2.0
Phase 3: Adaptive position sizing with drawdown guards, streak tracking,
and persistent JSON state across restarts.

FIXES vs v1:
  • update_balance() is now a separate public method; compute() no longer
    mutates peak balance (was a concurrency/ordering issue)
  • Position sizing formula clarified with LEVERAGE factor:
      contracts = (balance × risk_pct/100) / sl_distance_in_price
    This gives notional exposure, not leveraged. Binance futures position
    sizing uses SL distance in price, so leverage affects margin, not qty.
  • RiskState dataclass added for clean serialization
  • capital_status now returns enum-like strings for logging clarity
  • Added minimum position size guard (never places rounding-error qty)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import bot.config as cfg
from bot.utils import (
    clamp,
    get_logger,
    pct_change,
    round_step_size,
)

logger = get_logger("risk_engine")

MIN_MEANINGFUL_POSITION = 1e-6  # below this → don't trade


# ─────────────────────────────────────────────────────────────────────────────
# Capital Status Enum
# ─────────────────────────────────────────────────────────────────────────────

class CapitalStatus(str, Enum):
    NORMAL = "NORMAL"
    REDUCED = "REDUCED"       # soft risk reduction (low win rate / weak confluence)
    CONSECUTIVE_LOSS = "CONSECUTIVE_LOSS"
    PAUSED = "PAUSED"         # drawdown > DRAWDOWN_PAUSE_PCT
    HALTED = "HALTED"         # drawdown > DRAWDOWN_HALT_PCT


# ─────────────────────────────────────────────────────────────────────────────
# Persistent State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RiskState:
    peak_balance: float = 0.0
    current_balance: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    trades_today: int = 0
    last_reset_day: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─────────────────────────────────────────────────────────────────────────────
# Risk Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RiskResult:
    allowed: bool
    status: CapitalStatus
    risk_pct: float
    position_size: float     # in contracts/lots (rounded to step size)
    drawdown_pct: float
    reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Risk Engine
# ─────────────────────────────────────────────────────────────────────────────

class RiskEngine:
    """
    Adaptive risk sizing with drawdown protection.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self) -> None:
        self._state_path = Path(cfg.STATE_JSON_PATH)
        self._lock = asyncio.Lock()
        self.state = self._load_state()
        logger.info(
            "RiskEngine initialised | peak=%.2f current=%.2f cons_loss=%d",
            self.state.peak_balance,
            self.state.current_balance,
            self.state.consecutive_losses,
        )

    # ──────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────

    async def update_balance(self, balance: float) -> None:
        """
        FIX: Separated from compute(). Call this only when you have a
        confirmed, up-to-date balance from the exchange (after trade close
        or at startup). Never call inside evaluate loop.
        """
        async with self._lock:
            self.state.current_balance = balance
            if balance > self.state.peak_balance:
                self.state.peak_balance = balance
            await self._save_state()
            logger.debug("Balance updated: current=%.2f peak=%.2f", balance, self.state.peak_balance)

    async def compute(
        self,
        balance: float,
        sl_distance: float,
        step_size: float,
        reduce_size: bool = False,
    ) -> RiskResult:
        """
        Compute allowed position size for a new trade.

        Position sizing formula (SL-based):
          qty = (balance × risk_pct / 100) / sl_distance

        This calculates the notional contract quantity such that if price
        moves sl_distance against us, we lose exactly risk_pct% of balance.
        Leverage impacts required margin, not the qty/risk calculation.
        """
        async with self._lock:
            drawdown_pct = self._drawdown_pct(balance)
            status, risk_pct = self._determine_risk(
                balance, drawdown_pct, reduce_size
            )

            if status in (CapitalStatus.HALTED, CapitalStatus.PAUSED):
                return RiskResult(
                    allowed=False,
                    status=status,
                    risk_pct=0.0,
                    position_size=0.0,
                    drawdown_pct=drawdown_pct,
                    reason=f"Trading {status.value} — drawdown={drawdown_pct:.2f}%",
                )

            # Core sizing formula
            risk_amount = balance * risk_pct / 100.0
            if sl_distance <= 0:
                return RiskResult(
                    allowed=False,
                    status=status,
                    risk_pct=risk_pct,
                    position_size=0.0,
                    drawdown_pct=drawdown_pct,
                    reason="SL distance is zero — cannot size position",
                )

            raw_qty = risk_amount / sl_distance
            qty = round_step_size(raw_qty, step_size)

            if qty < MIN_MEANINGFUL_POSITION:
                return RiskResult(
                    allowed=False,
                    status=status,
                    risk_pct=risk_pct,
                    position_size=0.0,
                    drawdown_pct=drawdown_pct,
                    reason=f"Computed qty {raw_qty:.8f} below minimum after rounding",
                )

            logger.info(
                "Position sized | balance=%.2f risk=%.2f%% sl_dist=%.4f "
                "raw_qty=%.6f rounded=%.6f | status=%s drawdown=%.2f%%",
                balance, risk_pct, sl_distance, raw_qty, qty,
                status.value, drawdown_pct,
            )

            return RiskResult(
                allowed=True,
                status=status,
                risk_pct=risk_pct,
                position_size=qty,
                drawdown_pct=drawdown_pct,
            )

    async def record_win(self) -> None:
        async with self._lock:
            self.state.consecutive_losses = 0
            self.state.consecutive_wins += 1
            await self._save_state()

    async def record_loss(self) -> None:
        async with self._lock:
            self.state.consecutive_wins = 0
            self.state.consecutive_losses += 1
            await self._save_state()

    def get_drawdown_pct(self, balance: float) -> float:
        return self._drawdown_pct(balance)

    # ──────────────────────────────────────────────────────────
    # Internal Logic
    # ──────────────────────────────────────────────────────────

    def _drawdown_pct(self, balance: float) -> float:
        peak = self.state.peak_balance
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - balance) / peak * 100.0)

    def _determine_risk(
        self,
        balance: float,
        drawdown_pct: float,
        reduce_size: bool,
    ) -> tuple[CapitalStatus, float]:
        """Return (status, effective_risk_pct)."""

        # ── Hard stops ─────────────────────────────────────────
        if drawdown_pct >= cfg.DRAWDOWN_HALT_PCT:
            logger.critical(
                "TRADING HALTED: drawdown %.2f%% >= halt threshold %.2f%%",
                drawdown_pct, cfg.DRAWDOWN_HALT_PCT,
            )
            return CapitalStatus.HALTED, 0.0

        if drawdown_pct >= cfg.DRAWDOWN_PAUSE_PCT:
            logger.warning(
                "TRADING PAUSED: drawdown %.2f%% >= pause threshold %.2f%%",
                drawdown_pct, cfg.DRAWDOWN_PAUSE_PCT,
            )
            return CapitalStatus.PAUSED, 0.0

        # ── Adaptive risk ──────────────────────────────────────
        base = cfg.BASE_RISK_PCT

        # Consecutive-loss reduction (never increase risk after losses)
        if self.state.consecutive_losses >= cfg.CONSECUTIVE_LOSS_TRIGGER:
            risk = cfg.LOW_STREAK_RISK_PCT
            logger.info(
                "Consecutive losses (%d) → risk reduced to %.2f%%",
                self.state.consecutive_losses, risk,
            )
            status = CapitalStatus.CONSECUTIVE_LOSS
        else:
            risk = base
            status = CapitalStatus.NORMAL

        # Soft reduce from market context
        if reduce_size:
            risk = risk / 2.0
            status = CapitalStatus.REDUCED
            logger.info("Context soft-reduce → risk halved to %.2f%%", risk)

        # Hard cap — never exceed MAX_RISK_PCT regardless of logic above
        risk = min(risk, cfg.MAX_RISK_PCT)

        return status, risk

    # ──────────────────────────────────────────────────────────
    # State Persistence
    # ──────────────────────────────────────────────────────────

    def _load_state(self) -> RiskState:
        if self._state_path.exists():
            try:
                with open(self._state_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                state = RiskState.from_dict(data)
                logger.info(
                    "Loaded risk state from %s", self._state_path
                )
                return state
            except Exception as exc:
                logger.warning(
                    "Failed to load risk state (%s) — starting fresh", exc
                )
        return RiskState()

    async def _save_state(self) -> None:
        """Write state to disk (caller must hold self._lock)."""
        self.state.timestamp = time.time()
        try:
            tmp = self._state_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state.to_dict(), fh, indent=2)
            tmp.replace(self._state_path)  # atomic rename
        except Exception as exc:
            logger.error("Failed to save risk state: %s", exc)
