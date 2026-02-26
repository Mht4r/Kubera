"""
smc_engine.py  –  KUBERA 2.0
Smart Money Concepts confirmation layer (Phase 2.5)

Analyses OHLCV candle data to detect:
    • Order Blocks (OB)   – last opposing candle before strong impulse
    • Fair Value Gaps (FVG) – 3-candle imbalance zones
    • Support & Resistance (S/R) – swing-point clusters with 2+ touches
    • Trend Structure – HH/HL (bullish) vs LH/LL (bearish)
    • Retest Detection – price broke a level and returned to re-test it

Usage (called from SignalEngine.evaluate after Phase 2):
    result: SMCResult = await smc_engine.analyse(
        df_5m, df_1h, direction, entry_price, atr
    )
    # result.smc_score adds 0‒30 to confidence

Design principles:
    • Pure-Python / pandas — no external TA library required
    • All methods are static/deterministic — safe to unit-test
    • Conservative: only awards full score when multiple concepts align
    • Tolerant: works gracefully when candle history is short
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import bot.config as cfg
from bot.utils import get_logger

logger = get_logger("smc")


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SMCZone:
    """A detected price zone (OB or FVG)."""
    zone_high: float
    zone_low:  float
    zone_type: str          # "OB_BULL", "OB_BEAR", "FVG_BULL", "FVG_BEAR"
    strength:  float = 1.0  # 1.0 = normal, >1 = stronger
    timeframe: str  = "5m"

    @property
    def mid(self) -> float:
        return (self.zone_high + self.zone_low) / 2.0

    def contains(self, price: float, tolerance: float = 0.0) -> bool:
        return (self.zone_low - tolerance) <= price <= (self.zone_high + tolerance)


@dataclass
class SRLevel:
    """A detected support/resistance level."""
    price: float
    touches: int
    level_type: str   # "SUPPORT", "RESISTANCE", "BOTH"
    strength: float   # normalised touch count


@dataclass
class SMCResult:
    """Output of the SMC confirmation layer."""
    ob_zones:         list[SMCZone] = field(default_factory=list)
    fvg_zones:        list[SMCZone] = field(default_factory=list)
    sr_levels:        list[SRLevel] = field(default_factory=list)
    trend_structure:  str  = "NEUTRAL"   # BULLISH / BEARISH / NEUTRAL
    retest_detected:  bool = False
    price_at_ob:      bool = False
    price_at_fvg:     bool = False
    price_at_sr:      bool = False
    smc_score:        float = 0.0        # 0–30, added to confidence
    smc_notes:        list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"SMC={self.smc_score:.0f}/30 | Trend={self.trend_structure}"]
        if self.price_at_ob:
            parts.append("OB✓")
        if self.price_at_fvg:
            parts.append("FVG✓")
        if self.price_at_sr:
            parts.append("S/R✓")
        if self.retest_detected:
            parts.append("Retest✓")
        return " | ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# SMC Engine
# ─────────────────────────────────────────────────────────────────────────────

class SMCEngine:
    """
    Smart Money Concepts detection engine.

    All heavy lifting is done with vectorised pandas operations;
    no HTTP calls — data is provided by the caller (market_context).
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def analyse(
        self,
        df_5m:     pd.DataFrame,
        df_1h:     pd.DataFrame,
        direction: str,
        entry:     float,
        atr:       float,
    ) -> SMCResult:
        """
        Run all SMC detections and return a scored SMCResult.

        Score breakdown (max 30):
            • Trend alignment           : 0 – 10
            • At Order Block            : 0 – 8
            • At Fair Value Gap         : 0 – 6
            • At S/R level              : 0 – 4
            • Retest confirmed          : 0 – 2
        """
        result = SMCResult()
        tolerance = atr * cfg.SMC_ZONE_TOLERANCE_ATR if atr > 0 else entry * 0.001

        # ── 1. Trend structure (1h for bias, 5m for entry timing) ─────────
        h1_trend = self._detect_trend_structure(df_1h, lookback=cfg.SMC_SR_LOOKBACK)
        m5_trend = self._detect_trend_structure(df_5m, lookback=30)
        result.trend_structure = h1_trend

        trend_score = self._score_trend(direction, h1_trend, m5_trend)
        result.smc_score += trend_score
        if trend_score >= 8:
            result.smc_notes.append(f"Trend aligned (H1={h1_trend} M5={m5_trend})")
        elif trend_score <= 2:
            result.smc_notes.append(f"Trend OPPOSING (H1={h1_trend} M5={m5_trend})")

        # ── 2. Order Block detection ──────────────────────────────────────
        ob_zones = self.detect_order_blocks(df_5m, direction, lookback=cfg.SMC_OB_LOOKBACK)
        result.ob_zones = ob_zones

        # Also add H1 OBs for higher-timeframe confluence
        h1_obs = self.detect_order_blocks(df_1h, direction, lookback=cfg.SMC_OB_LOOKBACK // 5)
        ob_zones_all = ob_zones + h1_obs

        at_ob, ob_strength = self._price_at_zone(entry, ob_zones_all, tolerance)
        result.price_at_ob = at_ob
        if at_ob:
            ob_score = min(8.0, 4.0 + ob_strength * 2.0)
            result.smc_score += ob_score
            result.smc_notes.append(f"Price at OB zone (strength={ob_strength:.1f})")
        else:
            result.smc_notes.append("No OB zone at entry")

        # ── 3. Fair Value Gap detection ────────────────────────────────────
        fvg_zones = self.detect_fvg(df_5m, direction, lookback=cfg.SMC_FVG_LOOKBACK)
        result.fvg_zones = fvg_zones

        at_fvg, fvg_strength = self._price_at_zone(entry, fvg_zones, tolerance)
        result.price_at_fvg = at_fvg
        if at_fvg:
            fvg_score = min(6.0, 3.0 + fvg_strength * 1.5)
            result.smc_score += fvg_score
            result.smc_notes.append(f"Price inside FVG (strength={fvg_strength:.1f})")

        # ── 4. Support & Resistance ────────────────────────────────────────
        sr_levels = self.detect_sr_levels(
            df_1h, tolerance=tolerance, min_touches=cfg.SMC_SR_TOUCH_MIN
        )
        result.sr_levels = sr_levels

        at_sr, sr_strength = self._price_at_sr(entry, direction, sr_levels, tolerance)
        result.price_at_sr = at_sr
        if at_sr:
            sr_score = min(4.0, 2.0 + sr_strength)
            result.smc_score += sr_score
            result.smc_notes.append(f"Price at key S/R level (strength={sr_strength:.1f})")

        # ── 5. Retest detection ────────────────────────────────────────────
        retest = self._detect_retest(df_5m, entry, direction, tolerance)
        result.retest_detected = retest
        if retest:
            result.smc_score += 2.0
            result.smc_notes.append("Breakout-retest pattern confirmed")

        result.smc_score = min(result.smc_score, 30.0)

        logger.info(
            "SMC analysis: score=%.0f/30 trend=%s OB=%s FVG=%s SR=%s retest=%s",
            result.smc_score, result.trend_structure,
            result.price_at_ob, result.price_at_fvg,
            result.price_at_sr, result.retest_detected,
        )
        return result

    # ── Order Block Detection ─────────────────────────────────────────────────

    @staticmethod
    def detect_order_blocks(
        df: pd.DataFrame,
        direction: str,
        lookback: int = 50,
    ) -> list[SMCZone]:
        """
        Detect Order Blocks in the last `lookback` candles.

        Bullish OB (for BUY signals):
            Last bearish candle immediately BEFORE a strong bullish impulse
            (next candle body ≥ 1.5× ATR from the bearish candle's close).

        Bearish OB (for SELL signals):
            Last bullish candle immediately BEFORE a strong bearish impulse.

        Only the most recent 3 OBs are returned (most relevant to current price).
        """
        if len(df) < 5:
            return []

        data = df.iloc[-lookback:].reset_index(drop=True)
        zones: list[SMCZone] = []

        # ATR over this window for impulse threshold
        high = data["high"]
        low  = data["low"]
        prev_close = data["close"].shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = float(tr.ewm(span=14, adjust=False).mean().iloc[-1])
        impulse_threshold = atr * 1.5

        for i in range(len(data) - 2):
            curr = data.iloc[i]
            nxt  = data.iloc[i + 1]

            curr_body = curr["close"] - curr["open"]  # negative = bearish
            nxt_body  = nxt["close"]  - nxt["open"]

            if direction == "BUY":
                # Bearish candle followed by strong bullish impulse
                if curr_body < 0 and nxt_body > 0 and abs(nxt_body) >= impulse_threshold:
                    zones.append(SMCZone(
                        zone_high = float(curr["high"]),
                        zone_low  = float(curr["low"]),
                        zone_type = "OB_BULL",
                        strength  = min(abs(nxt_body) / (atr + 1e-9), 3.0),
                        timeframe = "5m",
                    ))
            else:  # SELL
                # Bullish candle followed by strong bearish drop
                if curr_body > 0 and nxt_body < 0 and abs(nxt_body) >= impulse_threshold:
                    zones.append(SMCZone(
                        zone_high = float(curr["high"]),
                        zone_low  = float(curr["low"]),
                        zone_type = "OB_BEAR",
                        strength  = min(abs(nxt_body) / (atr + 1e-9), 3.0),
                        timeframe = "5m",
                    ))

        # Return most recent (highest-index) OBs — most relevant
        return zones[-3:] if len(zones) > 3 else zones

    # ── Fair Value Gap Detection ──────────────────────────────────────────────

    @staticmethod
    def detect_fvg(
        df: pd.DataFrame,
        direction: str,
        lookback: int = 30,
    ) -> list[SMCZone]:
        """
        Detect Fair Value Gaps using the 3-candle structure.

        Bullish FVG (BUY): candle[i].high < candle[i+2].low
            → gap between wick of candle i and wick of candle i+2

        Bearish FVG (SELL): candle[i].low > candle[i+2].high
            → gap between wick of candle i and wick of candle i+2

        Only unfilled gaps (gap still exists at current price range) are kept.
        """
        if len(df) < 4:
            return []

        data = df.iloc[-lookback:].reset_index(drop=True)
        zones: list[SMCZone] = []
        current_price = float(data["close"].iloc[-1])

        for i in range(len(data) - 2):
            c0, c1, c2 = data.iloc[i], data.iloc[i + 1], data.iloc[i + 2]

            if direction == "BUY":
                # Bullish FVG: gap above candle 0, below candle 2
                if float(c0["high"]) < float(c2["low"]):
                    gap_low  = float(c0["high"])
                    gap_high = float(c2["low"])
                    # Only keep if not yet filled (current price above or inside gap)
                    if current_price <= gap_high * 1.02:
                        strength = (gap_high - gap_low) / (float(c1["high"]) - float(c1["low"]) + 1e-9)
                        zones.append(SMCZone(
                            zone_high = gap_high,
                            zone_low  = gap_low,
                            zone_type = "FVG_BULL",
                            strength  = min(strength, 3.0),
                        ))

            else:  # SELL
                # Bearish FVG: gap below candle 0, above candle 2
                if float(c0["low"]) > float(c2["high"]):
                    gap_high = float(c0["low"])
                    gap_low  = float(c2["high"])
                    # Only keep if not yet filled
                    if current_price >= gap_low * 0.98:
                        strength = (gap_high - gap_low) / (float(c1["high"]) - float(c1["low"]) + 1e-9)
                        zones.append(SMCZone(
                            zone_high = gap_high,
                            zone_low  = gap_low,
                            zone_type = "FVG_BEAR",
                            strength  = min(strength, 3.0),
                        ))

        return zones[-5:] if len(zones) > 5 else zones

    # ── Support & Resistance Level Detection ──────────────────────────────────

    @staticmethod
    def detect_sr_levels(
        df: pd.DataFrame,
        tolerance: float = 0.0,
        min_touches: int = 2,
    ) -> list[SRLevel]:
        """
        Detect horizontal S/R zones by clustering swing highs/lows.

        Algorithm:
          1. Find all swing highs (local max over ±2 bars) and lows (local min)
          2. Cluster nearby pivots (within `tolerance`) together
          3. Clusters with >= `min_touches` touches are key levels

        Returns levels sorted by strength (touch count) descending.
        """
        if len(df) < 10:
            return []

        lookback = min(len(df), cfg.SMC_SR_LOOKBACK)
        data = df.iloc[-lookback:]

        # Find swing highs and lows
        pivots: list[tuple[float, str]] = []
        for i in range(2, len(data) - 2):
            row = data.iloc[i]
            ph = float(data.iloc[i]["high"])
            pl = float(data.iloc[i]["low"])

            # Swing high: highest of 5 bars
            if all(ph >= float(data.iloc[i + j]["high"]) for j in (-2, -1, 1, 2)):
                pivots.append((ph, "HIGH"))
            # Swing low: lowest of 5 bars
            if all(pl <= float(data.iloc[i + j]["low"]) for j in (-2, -1, 1, 2)):
                pivots.append((pl, "LOW"))

        if not pivots:
            return []

        # Cluster pivots that are within tolerance of each other
        if tolerance <= 0:
            # Default: 0.1% of price
            ref_price = float(df["close"].iloc[-1])
            tolerance = ref_price * 0.001

        clusters: list[list[tuple[float, str]]] = []
        for price, ptype in sorted(pivots, key=lambda x: x[0]):
            placed = False
            for cluster in clusters:
                cluster_mid = sum(p for p, _ in cluster) / len(cluster)
                if abs(price - cluster_mid) <= tolerance:
                    cluster.append((price, ptype))
                    placed = True
                    break
            if not placed:
                clusters.append([(price, ptype)])

        # Build SRLevel for clusters with enough touches
        levels: list[SRLevel] = []
        for cluster in clusters:
            if len(cluster) < min_touches:
                continue
            prices = [p for p, _ in cluster]
            types  = {t for _, t in cluster}
            level_price = sum(prices) / len(prices)
            ltype = "BOTH" if ("HIGH" in types and "LOW" in types) else (
                "RESISTANCE" if "HIGH" in types else "SUPPORT"
            )
            levels.append(SRLevel(
                price      = level_price,
                touches    = len(cluster),
                level_type = ltype,
                strength   = min(float(len(cluster)) / 2.0, 3.0),
            ))

        return sorted(levels, key=lambda x: x.strength, reverse=True)

    # ── Trend Structure ───────────────────────────────────────────────────────

    @staticmethod
    def _detect_trend_structure(df: pd.DataFrame, lookback: int = 50) -> str:
        """
        Determine market structure: BULLISH (HH+HL), BEARISH (LH+LL), or NEUTRAL.

        Method: identify the last 4 swing highs and 4 swing lows, check sequence.
        """
        if len(df) < max(lookback, 10):
            return "NEUTRAL"

        data = df.iloc[-lookback:]
        swing_highs: list[float] = []
        swing_lows:  list[float] = []

        for i in range(2, len(data) - 2):
            h = float(data.iloc[i]["high"])
            lo = float(data.iloc[i]["low"])

            if all(h >= float(data.iloc[i + j]["high"]) for j in (-2, -1, 1, 2)):
                swing_highs.append(h)
            if all(lo <= float(data.iloc[i + j]["low"]) for j in (-2, -1, 1, 2)):
                swing_lows.append(lo)

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "NEUTRAL"

        # Check last 3 pivots each
        hh = all(swing_highs[i] > swing_highs[i - 1] for i in range(1, min(3, len(swing_highs))))
        hl = all(swing_lows[i] > swing_lows[i - 1] for i in range(1, min(3, len(swing_lows))))
        lh = all(swing_highs[i] < swing_highs[i - 1] for i in range(1, min(3, len(swing_highs))))
        ll = all(swing_lows[i] < swing_lows[i - 1] for i in range(1, min(3, len(swing_lows))))

        if hh and hl:
            return "BULLISH"
        if lh and ll:
            return "BEARISH"
        return "NEUTRAL"

    # ── Retest Detection ──────────────────────────────────────────────────────

    @staticmethod
    def _detect_retest(
        df: pd.DataFrame,
        entry: float,
        direction: str,
        tolerance: float,
    ) -> bool:
        """
        Detect if price recently broke a prior level and has now come back to retest it.

        Simplified: look at last 20 candles for a break-retest pattern:
          - SELL retest: candles made a lower low, then price bounced back toward
            the broken support (now resistance) zone near entry
          - BUY retest: candles made a higher high, then price retraced toward
            the broken resistance (now support) zone near entry
        """
        if len(df) < 10:
            return False

        lookback_df = df.iloc[-20:]
        highs = lookback_df["high"].values.tolist()
        lows  = lookback_df["low"].values.tolist()

        # Look for break + return within last 20 candles
        level_high = entry + tolerance
        level_low  = entry - tolerance

        if direction == "BUY":
            # Price previously broke above our zone (made new high), now returning
            peak_idx = highs.index(max(highs))
            if peak_idx < len(highs) - 3:
                # Price went above zone and came back
                broke_above = any(h > level_high for h in highs[:peak_idx + 1])
                returned    = any(lo <= level_high * 1.001 for lo in lows[peak_idx:])
                return broke_above and returned

        else:  # SELL
            # Price previously broke below our zone, now retesting from below
            trough_idx = lows.index(min(lows))
            if trough_idx < len(lows) - 3:
                broke_below = any(lo < level_low for lo in lows[:trough_idx + 1])
                returned    = any(h >= level_low * 0.999 for h in highs[trough_idx:])
                return broke_below and returned

        return False

    # ── Scoring Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _score_trend(direction: str, h1_trend: str, m5_trend: str) -> float:
        """
        Score 0–10 for trend alignment.
          • Both H1 and 5m aligned = 10
          • Only H1 aligned         = 7
          • Both neutral            = 4
          • Opposing trend          = 0 (hard reject handled by market_context)
        """
        h1_aligned = (
            (direction == "BUY"  and h1_trend == "BULLISH") or
            (direction == "SELL" and h1_trend == "BEARISH")
        )
        m5_aligned = (
            (direction == "BUY"  and m5_trend == "BULLISH") or
            (direction == "SELL" and m5_trend == "BEARISH")
        )
        h1_opposing = (
            (direction == "BUY"  and h1_trend == "BEARISH") or
            (direction == "SELL" and h1_trend == "BULLISH")
        )

        if h1_opposing:
            return 0.0     # market_context handles hard-reject; score 0 here too
        if h1_aligned and m5_aligned:
            return 10.0
        if h1_aligned:
            return 7.0
        if m5_aligned:
            return 4.0
        return 3.0         # neutral

    @staticmethod
    def _price_at_zone(
        price: float,
        zones: list[SMCZone],
        tolerance: float,
    ) -> tuple[bool, float]:
        """
        Check if `price` is inside or within `tolerance` of any zone.
        Returns (hit, max_strength).
        """
        best_strength = 0.0
        for z in zones:
            if z.contains(price, tolerance):
                best_strength = max(best_strength, z.strength)
        return (best_strength > 0), best_strength

    @staticmethod
    def _price_at_sr(
        price: float,
        direction: str,
        levels: list[SRLevel],
        tolerance: float,
    ) -> tuple[bool, float]:
        """
        Check if price is near a relevant S/R level for the signal direction.
          SELL signal: price near RESISTANCE or BOTH
          BUY signal:  price near SUPPORT or BOTH
        """
        for lvl in levels:
            if abs(price - lvl.price) <= tolerance:
                if direction == "SELL" and lvl.level_type in ("RESISTANCE", "BOTH"):
                    return True, lvl.strength
                if direction == "BUY" and lvl.level_type in ("SUPPORT", "BOTH"):
                    return True, lvl.strength
        return False, 0.0
