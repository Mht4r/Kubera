"""
signal_engine.py  –  KUBERA 2.0
Phase 1 (Signal Decomposition) + Phase 2 (Context Filter)

FIXES vs v1:
  • _build_justification now uses self._min_rr (was hardcoded to cfg.MIN_RR
    even in strict mode)
  • _optimize: single ordered pass (entry → SL → TP); no double RR inflation
  • _optimize: tps is a fresh list copy, never mutates the raw signal
  • _reject helper accepts direction safely even on decompose failure
  • SignalDecision.summary() added Capital Status formatting
  • Added blended_rr re-check after spread validation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from bot.market_context import MarketContext, MarketContextResult
from bot.smc_engine import SMCEngine, SMCResult
from bot.utils import get_logger, weighted_average
import bot.config as cfg

logger = get_logger("signal_engine")


# ─────────────────────────────────────────────────────────────────────────────
# Input Signal (canonical internal format — from signal_parser)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RawSignal:
    symbol: str
    direction: str    # "BUY" or "SELL"
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profits: list[float]
    news_flag: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "RawSignal":
        el = float(d.get("entry_low", d.get("entry", 0)))
        eh = float(d.get("entry_high", d.get("entry", 0)))
        if el > eh:
            el, eh = eh, el
        return cls(
            symbol=d["symbol"].upper(),
            direction=d["direction"].upper(),
            entry_low=el,
            entry_high=eh,
            stop_loss=float(d["stop_loss"]),
            take_profits=[float(t) for t in d["take_profits"]],
            news_flag=bool(d.get("news_flag", False)),
        )

    @classmethod
    def from_parsed(cls, parsed) -> "RawSignal":
        """Create from signal_parser.ParsedSignal."""
        return cls(
            symbol=parsed.symbol,
            direction=parsed.direction,
            entry_low=parsed.entry_low,
            entry_high=parsed.entry_high,
            stop_loss=parsed.stop_loss,
            take_profits=list(parsed.take_profits),
            news_flag=parsed.news_flag,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalDecision:
    action: str                          # EXECUTE / OPTIMIZE / REJECT
    symbol: str
    direction: str
    entry: float                         # optimized entry price
    entry_low: float = 0.0              # original signal zone low
    entry_high: float = 0.0             # original signal zone high
    stop_loss: float = 0.0              # optimized SL
    take_profits: list[float] = field(default_factory=list)  # optimized TPs
    sl_distance: float = 0.0
    blended_rr: float = 0.0
    per_tp_rr: list[float] = field(default_factory=list)
    risk_pct: float = 0.0                # filled by RiskEngine
    position_size: float = 0.0          # filled by RiskEngine
    confidence: float = 0.0
    context: Optional[MarketContextResult] = None
    smc_result: Optional[SMCResult] = None   # Phase 2.5 SMC analysis
    reduce_size: bool = False
    reject_reason: Optional[str] = None
    optimization_applied: bool = False
    justification: str = ""

    def summary(self) -> str:
        capital_status = "PAUSED" if self.action == "REJECT" else (
            "REDUCED" if self.reduce_size else "NORMAL"
        )
        lines = [
            "─" * 64,
            f"  ██ KUBERA 2.0 — TRADE DECISION: {self.action}",
            "─" * 64,
            f"  Symbol          : {self.symbol}",
            f"  Direction       : {self.direction}",
            f"  Entry           : {self.entry:.4f}" + (" [OPTIMIZED]" if self.optimization_applied else ""),
            f"  Stop Loss       : {self.stop_loss:.4f}",
            f"  Take Profits    : {[f'{t:.4f}' for t in self.take_profits]}",
            f"  SL Distance     : {self.sl_distance:.4f}",
            f"  Blended RR      : 1:{self.blended_rr:.2f}",
            f"  Per-TP RR       : {[f'1:{r:.2f}' for r in self.per_tp_rr]}",
            f"  Confidence      : {self.confidence:.1f} / 100",
            f"  Risk %          : {self.risk_pct:.2f}%",
            f"  Position Size   : {self.position_size:.6f}",
            f"  Capital Status  : {capital_status}",
            f"  Justification   : {self.justification}",
        ]
        if self.reject_reason:
            lines.append(f"  Reject Reason   : {self.reject_reason}")
        lines.append("─" * 64)
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Signal Engine
# ─────────────────────────────────────────────────────────────────────────────

class SignalEngine:
    """
    6-phase quantitative intelligence pipeline — Phase 1 & 2.
    Evaluates, optimises, and filters signals before capital is deployed.
    """

    def __init__(self, market_context: MarketContext) -> None:
        self.market = market_context
        self._smc = SMCEngine()
        self.strict_mode: bool = False
        self._min_rr: float = cfg.MIN_RR

    def enable_strict_mode(self) -> None:
        logger.warning(
            "STRICT MODE activated — minimum RR raised to %.1f", cfg.MIN_RR_STRICT
        )
        self.strict_mode = True
        self._min_rr = cfg.MIN_RR_STRICT

    def disable_strict_mode(self) -> None:
        logger.info("Strict mode deactivated — minimum RR restored to %.1f", cfg.MIN_RR)
        self.strict_mode = False
        self._min_rr = cfg.MIN_RR

    # ──────────────────────────────────────────────────────────
    # Main Entry Point
    # ──────────────────────────────────────────────────────────

    async def evaluate(self, raw: RawSignal) -> SignalDecision:
        """Run Phase 1 (decomposition) + Phase 2 (context filter)."""
        logger.info(
            "[%s] ── Evaluating: %s entry=[%.4f–%.4f] SL=%.4f TPs=%s ──",
            raw.symbol, raw.direction, raw.entry_low, raw.entry_high,
            raw.stop_loss, raw.take_profits,
        )

        # ── Phase 1: Signal Decomposition ──────────────────────
        decision = self._decompose(raw)
        if decision.action == "REJECT":
            logger.warning("[%s] REJECTED (Phase 1): %s", raw.symbol, decision.reject_reason)
            return decision

        # ── Phase 2: Market Context Filter ─────────────────────
        ctx = await self.market.analyse(
            symbol=raw.symbol,
            direction=raw.direction,
            entry_mid=decision.entry,
            sl_distance=decision.sl_distance,
            tp1=decision.take_profits[0] if decision.take_profits else None,
            news_flag=raw.news_flag,
        )
        decision.context = ctx

        if ctx.reject_reason:
            decision.action = "REJECT"
            decision.reject_reason = ctx.reject_reason
            decision.justification = f"Context hard-reject: {ctx.reject_reason}"
            logger.warning("[%s] REJECTED (Phase 2): %s", raw.symbol, ctx.reject_reason)
            return decision

        # ── Phase 2.5: SMC Confirmation ────────────────────────────
        if cfg.SMC_ENABLED:
            try:
                df_5m = await self.market.get_candles(raw.symbol, "5m")
                df_1h = await self.market.get_candles(raw.symbol, "1h")
                smc = self._smc.analyse(
                    df_5m=df_5m,
                    df_1h=df_1h,
                    direction=raw.direction,
                    entry=decision.entry,
                    atr=ctx.atr,
                )
                decision.smc_result = smc
                logger.info(
                    "[%s] SMC: %s", raw.symbol, smc.summary()
                )
            except Exception as exc:
                logger.warning("[%s] SMC analysis failed (non-fatal): %s", raw.symbol, exc)
                decision.smc_result = None
        else:
            decision.smc_result = None

        # Geometry score requires RR (only available after Phase 1)
        ctx.geometry_score = self._geometry_score(
            decision.blended_rr, decision.sl_distance, ctx.atr
        )
        ctx.compute_confidence()

        # Fold SMC score in (max confidence is now 130 before cap; still cap at 100)
        base_conf = ctx.confidence
        smc_bonus  = decision.smc_result.smc_score if decision.smc_result else 0.0
        decision.confidence = min(base_conf + smc_bonus, 100.0)

        logger.info("[%s] Confidence: %.1f/100 (base=%.0f smc=+%.0f)",
                    raw.symbol, decision.confidence, base_conf, smc_bonus)

        if ctx.confidence < cfg.CONFIDENCE_REJECT_THRESHOLD:
            decision.action = "REJECT"
            decision.reject_reason = (
                f"LOW_CONFIDENCE_{ctx.confidence:.0f}_BELOW_{cfg.CONFIDENCE_REJECT_THRESHOLD}"
            )
            decision.justification = "Composite market score below minimum threshold."
            return decision

        # Soft reduce
        if ctx.confidence < cfg.CONFIDENCE_REDUCE_THRESHOLD:
            decision.reduce_size = True
            logger.info(
                "[%s] Confidence %.1f below reduce threshold %.0f → position halved",
                raw.symbol, ctx.confidence, cfg.CONFIDENCE_REDUCE_THRESHOLD,
            )

        # Spread check
        if decision.sl_distance > 0:
            spread_pct = ctx.spread / decision.sl_distance * 100
            if spread_pct > cfg.MAX_SPREAD_PCT_OF_SL:
                decision.action = "REJECT"
                decision.reject_reason = f"SPREAD_TOO_WIDE_{spread_pct:.1f}PCT_OF_SL"
                decision.justification = "Spread exceeds allowable % of SL distance."
                return decision

        decision.action = "OPTIMIZE" if decision.optimization_applied else "EXECUTE"
        decision.justification = self._build_justification(decision, ctx)

        logger.info("[%s] Decision: %s | RR=1:%.2f | Conf=%.0f",
                    raw.symbol, decision.action, decision.blended_rr, ctx.confidence)
        return decision

    # ──────────────────────────────────────────────────────────
    # Phase 1 — Signal Decomposition
    # ──────────────────────────────────────────────────────────

    def _decompose(self, raw: RawSignal) -> SignalDecision:
        """Parse signal geometry and attempt RR optimization."""
        entry_mid = (raw.entry_low + raw.entry_high) / 2.0
        sl_dist = self._sl_distance(raw.direction, entry_mid, raw.stop_loss)

        if sl_dist <= 0:
            return self._reject(raw, "INVALID_SL", "SL distance is zero or on wrong side of entry.")

        # Work on fresh copies — never mutate raw signal
        entry = entry_mid
        sl = raw.stop_loss
        tps = list(raw.take_profits)  # FIX: guaranteed new list, not reference

        per_tp_rr, blended_rr = self._compute_rr(raw.direction, entry, sl_dist, tps)
        optimized = False

        if blended_rr < self._min_rr:
            logger.info(
                "[%s] Blended RR %.2f below %.1f — attempting optimization",
                raw.symbol, blended_rr, self._min_rr,
            )
            entry, sl, tps, blended_rr, sl_dist, per_tp_rr = self._optimize(
                raw, entry_mid
            )
            optimized = True
            if blended_rr < self._min_rr:
                return self._reject(
                    raw,
                    "INSUFFICIENT_RR",
                    f"Blended RR {blended_rr:.2f} < min {self._min_rr:.1f} after optimization.",
                )

        return SignalDecision(
            action="PENDING",
            symbol=raw.symbol,
            direction=raw.direction,
            entry=entry,
            entry_low=raw.entry_low,
            entry_high=raw.entry_high,
            stop_loss=sl,
            take_profits=tps,
            sl_distance=sl_dist,
            blended_rr=blended_rr,
            per_tp_rr=per_tp_rr,
            optimization_applied=optimized,
        )

    def _optimize(
        self, raw: RawSignal, entry_mid: float
    ) -> tuple[float, float, list[float], float, float, list[float]]:
        """
        FIX vs v1: Single ordered optimization pass — no double RR inflation.

        Levers (applied in order):
          1. Better zone boundary for entry (±20% of zone width)
          2. SL tightened by 5% toward entry
          3. Last TP extended by 20% of TP-to-entry distance

        Returns (entry, sl, tps, blended_rr, sl_dist, per_tp_rr)
        """
        direction = raw.direction
        tps = list(raw.take_profits)  # fresh copy

        # Lever 1: better boundary entry
        zone_half = (raw.entry_high - raw.entry_low) / 2.0
        if direction == "BUY":
            entry = raw.entry_low + zone_half * 0.2   # 20% from low edge
        else:
            entry = raw.entry_high - zone_half * 0.2  # 20% from high edge

        sl = raw.stop_loss
        sl_dist = self._sl_distance(direction, entry, sl)
        if sl_dist <= 0:
            # Fallback: use midpoint
            entry = entry_mid
            sl_dist = self._sl_distance(direction, entry, sl)

        # Lever 2: tighten SL by 5%
        tightened_entry, tightened_sl = entry, sl
        move = sl_dist * 0.05
        if direction == "BUY":
            tightened_sl = sl + move   # SL moves up (closer to entry)
        else:
            tightened_sl = sl - move   # SL moves down (closer to entry)
        tightened_dist = self._sl_distance(direction, tightened_entry, tightened_sl)
        if tightened_dist > 0:
            _, test_rr = self._compute_rr(direction, tightened_entry, tightened_dist, tps)
            if test_rr >= self._min_rr:
                sl, sl_dist = tightened_sl, tightened_dist
                entry = tightened_entry

        # Lever 3: extend last TP by 20% of TP1-to-entry distance
        if tps:
            anchor_tp = tps[0]
            extension = abs(anchor_tp - entry) * 0.20
            if direction == "BUY":
                tps[-1] = tps[-1] + extension
            else:
                tps[-1] = tps[-1] - extension

        per_tp_rr, blended_rr = self._compute_rr(direction, entry, sl_dist, tps)
        return entry, sl, tps, blended_rr, sl_dist, per_tp_rr

    # ──────────────────────────────────────────────────────────
    # RR / Geometry Helpers
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _sl_distance(direction: str, entry: float, sl: float) -> float:
        dist = (entry - sl) if direction == "BUY" else (sl - entry)
        return max(dist, 0.0)

    @staticmethod
    def _compute_rr(
        direction: str, entry: float, sl_dist: float, tps: list[float]
    ) -> tuple[list[float], float]:
        if sl_dist <= 0 or not tps:
            return [], 0.0
        per: list[float] = []
        for tp in tps:
            reward = (tp - entry) if direction == "BUY" else (entry - tp)
            per.append(max(reward / sl_dist, 0.0))
        blended = weighted_average(per, list(cfg.TP_WEIGHTS))
        return per, blended

    @staticmethod
    def _geometry_score(blended_rr: float, sl_dist: float, atr: float) -> float:
        """Score 0–20: RR quality (0–10) + SL/ATR ratio (0–10)."""
        rr_score = min((blended_rr / 3.0) * 10, 10.0)
        if atr > 0:
            ratio = sl_dist / atr
            if 0.5 <= ratio <= 2.0:
                atr_score = 10.0
            elif ratio < 0.5:
                atr_score = 4.0   # too tight — stop-hunt risk
            elif ratio <= 4.0:
                atr_score = 6.0
            else:
                atr_score = 2.0   # oversized SL
        else:
            atr_score = 5.0
        return rr_score + atr_score

    # ──────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _reject(raw: RawSignal, code: str, reason: str) -> SignalDecision:
        return SignalDecision(
            action="REJECT",
            symbol=raw.symbol,
            direction=raw.direction,
            entry=0.0,
            entry_low=raw.entry_low,
            entry_high=raw.entry_high,
            stop_loss=raw.stop_loss,
            take_profits=list(raw.take_profits),
            sl_distance=0.0,
            blended_rr=0.0,
            per_tp_rr=[],
            reject_reason=code,
            justification=reason,
        )

    def _build_justification(self, decision: SignalDecision, ctx: MarketContextResult) -> str:
        # FIX: use self._min_rr, not cfg.MIN_RR (different in strict mode)
        parts = [
            f"RR=1:{decision.blended_rr:.2f} (min 1:{self._min_rr:.1f})",
            f"Confidence={decision.confidence:.0f}/100",
            f"HTF={ctx.htf_trend}",
            f"ATR={ctx.atr:.4f}",
            f"VolRatio={ctx.volume_ratio:.2f}",
        ]
        if decision.smc_result:
            parts.append(decision.smc_result.summary())
        if decision.optimization_applied:
            parts.append("Entry/SL optimized")
        if decision.reduce_size:
            parts.append("Size reduced 50% (weak alignment)")
        if self.strict_mode:
            parts.append("STRICT_MODE")
        return " | ".join(parts)
