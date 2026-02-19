"""
market_context.py  –  KUBERA 2.0
Multi-timeframe market analysis: candle fetch, indicator computation,
and 0–100 confidence scoring.

FIXES vs v1:
  • `import time` moved to module level (was inside _get_candles causing
    repeated module lookups per call)
  • `_score_momentum` now uses 5m frame for RSI and EMA slope instead of
    1m (1m is too noisy; 5m is actionable for entry timing)
  • `_get_candles` cache key uses ttl correctly, avoids stale data
  • Hard-reject for STRONG opposing trend now requires 3/3 EMA alignment,
    not just any BEAR vs BUY mismatch (was too aggressive before)
  • `MarketContextResult.atr` is guaranteed non-None
  • Added `last_candle_body_pct` to result for entry filter use
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from binance import AsyncClient

import bot.config as cfg
from bot.utils import async_retry, get_logger

logger = get_logger("market_context")


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarketContextResult:
    symbol: str
    price: float
    spread: float
    atr: float               # ATR(14) on 1m
    atr_avg20: float         # 20-period mean of ATR
    ema20: float             # 1h EMA20
    ema50: float             # 1h EMA50
    ema200: float            # 1h EMA200
    trend_score: float       # 0–25
    momentum_score: float    # 0–25
    volatility_score: float  # 0–15
    volume_score: float      # 0–15
    geometry_score: float = 0.0   # 0–20 (filled by SignalEngine)
    confidence: float = 0.0       # 0–100 (finalised after geometry)
    htf_trend: str = "NEUTRAL"    # BULL / BEAR / NEUTRAL
    is_volatile: bool = False
    volume_ratio: float = 1.0
    last_candle_body_pct: float = 0.0  # body as % of full range (0–1)
    reject_reason: Optional[str] = None

    def compute_confidence(self) -> float:
        self.confidence = (
            self.trend_score
            + self.momentum_score
            + self.volatility_score
            + self.volume_score
            + self.geometry_score
        )
        return self.confidence


# ─────────────────────────────────────────────────────────────────────────────
# MarketContext class
# ─────────────────────────────────────────────────────────────────────────────

class MarketContext:
    """Fetches Binance Futures OHLCV data and scores market conditions."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        # Cache: symbol+tf → (fetch_ts, DataFrame)
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._cache_ttl: float = 30.0  # seconds per timeframe

    # ──────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────

    async def analyse(
        self,
        symbol: str,
        direction: str,
        entry_mid: float,
        sl_distance: float,
        tp1: Optional[float],
        news_flag: bool = False,
    ) -> MarketContextResult:
        """
        Fetch multi-TF data and return a MarketContextResult with partial
        confidence scores. Geometry score is set externally by SignalEngine
        once RR is known.
        """
        logger.info("[%s] Fetching market context ...", symbol)

        # Fetch all timeframes concurrently
        frames: list[pd.DataFrame] = await asyncio.gather(
            *[self._get_candles(symbol, tf) for tf in cfg.TIMEFRAMES]
        )
        df_1m, df_5m, df_1h = frames

        # ── Indicators ─────────────────────────────────────────
        atr_series = self._atr(df_1m, cfg.ATR_PERIOD)
        atr = float(atr_series.iloc[-1])
        atr_avg20 = float(atr_series.iloc[-20:].mean())

        # HTF structure uses 1h frame
        ema20 = self._ema_val(df_1h["close"], cfg.EMA_FAST)
        ema50 = self._ema_val(df_1h["close"], cfg.EMA_MID)
        ema200 = self._ema_val(df_1h["close"], cfg.EMA_SLOW)
        price = float(df_1m["close"].iloc[-1])

        # Spread approximated from 1m high-low of last candle
        spread = float(df_1m["high"].iloc[-1] - df_1m["low"].iloc[-1])

        htf_trend, strong_opposing = self._classify_trend(
            price, ema20, ema50, ema200, direction
        )
        vol_ratio = self._volume_ratio(df_1m)
        is_volatile = atr > atr_avg20 * 1.8

        # Body % of last 1m candle (for entry quality)
        last = df_1m.iloc[-1]
        candle_range = last["high"] - last["low"]
        body_pct = (abs(last["close"] - last["open"]) / candle_range) if candle_range > 0 else 0.0

        # ── Scores ─────────────────────────────────────────────
        trend_score = self._score_trend(direction, htf_trend)
        # FIX: momentum uses 5m frame for more meaningful slope
        momentum_score = self._score_momentum(df_5m, direction)
        volatility_score = self._score_volatility(atr, atr_avg20)
        volume_score = self._score_volume(vol_ratio, direction, df_1m)

        result = MarketContextResult(
            symbol=symbol,
            price=price,
            spread=spread,
            atr=atr,
            atr_avg20=atr_avg20,
            ema20=ema20,
            ema50=ema50,
            ema200=ema200,
            trend_score=trend_score,
            momentum_score=momentum_score,
            volatility_score=volatility_score,
            volume_score=volume_score,
            htf_trend=htf_trend,
            is_volatile=is_volatile,
            volume_ratio=vol_ratio,
            last_candle_body_pct=body_pct,
        )

        # ── Hard-reject checks ─────────────────────────────────
        if news_flag:
            result.reject_reason = "NEWS_RISK_FLAG"
            logger.warning("[%s] Rejected: high-impact news flag active", symbol)
            return result

        # FIX: only hard-reject on STRONG opposing trend (3/3 EMA alignment)
        if strong_opposing:
            result.reject_reason = "STRONG_HTF_OPPOSING_TREND"
            logger.warning("[%s] Rejected: all 3 EMAs oppose signal direction", symbol)
            return result

        if atr > atr_avg20 * 2.5:
            result.reject_reason = "EXTREME_VOLATILITY"
            logger.warning(
                "[%s] Rejected: ATR spike %.4f vs avg %.4f (%.1f×)",
                symbol, atr, atr_avg20, atr / atr_avg20,
            )
            return result

        # Price already moved > MAX_PRICE_MOVED_TO_TP_PCT% toward TP1
        if tp1 is not None and sl_distance > 0:
            reward_full = abs(tp1 - entry_mid)
            if reward_full > 0:
                moved = abs(price - entry_mid)
                moved_pct = moved / reward_full * 100
                if moved_pct > cfg.MAX_PRICE_MOVED_TO_TP_PCT:
                    result.reject_reason = (
                        f"PRICE_MOVED_{moved_pct:.0f}PCT_TOWARD_TP1"
                    )
                    logger.warning(
                        "[%s] Rejected: price moved %.0f%% toward TP1 (max %.0f%%)",
                        symbol, moved_pct, cfg.MAX_PRICE_MOVED_TO_TP_PCT,
                    )
                    return result

        logger.info(
            "[%s] Context → Trend=%.0f Mom=%.0f Vol=%.0f Volume=%.0f | HTF=%s ATR=%.4f VolRatio=%.2f",
            symbol, trend_score, momentum_score, volatility_score, volume_score,
            htf_trend, atr, vol_ratio,
        )
        return result

    # ──────────────────────────────────────────────────────────
    # Candle Fetching with Cache
    # ──────────────────────────────────────────────────────────

    @async_retry()
    async def _get_candles(self, symbol: str, interval: str) -> pd.DataFrame:
        """Fetch OHLCV candles with per-timeframe TTL cache."""
        cache_key = f"{symbol}:{interval}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]

        raw = await self.client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=cfg.CANDLE_LIMIT,
        )
        df = self._raw_to_df(raw)
        self._cache[cache_key] = (time.time(), df)
        return df

    def invalidate_cache(self, symbol: Optional[str] = None) -> None:
        """Invalidate cache for one symbol or all symbols."""
        if symbol:
            keys = [k for k in self._cache if k.startswith(f"{symbol}:")]
            for k in keys:
                del self._cache[k]
        else:
            self._cache.clear()

    # ──────────────────────────────────────────────────────────
    # Indicator Computations (all static for testability)
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _raw_to_df(raw: list) -> pd.DataFrame:
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base", "taker_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df.set_index("open_time", inplace=True)
        return df

    @staticmethod
    def _ema_val(series: pd.Series, period: int) -> float:
        """Return the last EMA value."""
        return float(series.ewm(span=period, adjust=False).mean().iloc[-1])

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        """Compute ATR(period) series using EWM smoothing."""
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int = 14) -> float:
        """Wilder RSI for the most recent candle."""
        delta = series.diff()
        gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
        rs = gain / (loss + 1e-9)
        return float(100 - 100 / (1 + rs.iloc[-1]))

    @staticmethod
    def _volume_ratio(df: pd.DataFrame) -> float:
        """Current candle volume / 20-period average volume (excluding current)."""
        avg = float(df["volume"].iloc[-21:-1].mean())
        cur = float(df["volume"].iloc[-1])
        return cur / (avg + 1e-9)

    # ──────────────────────────────────────────────────────────
    # Trend Classification
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _classify_trend(
        price: float,
        ema20: float,
        ema50: float,
        ema200: float,
        direction: str,
    ) -> tuple[str, bool]:
        """
        Returns (htf_trend, strong_opposing).

        strong_opposing is True ONLY when all 3 EMA conditions unanimously
        oppose the signal direction (was too aggressive with 2/3 before).
        """
        bull = [price > ema20, ema20 > ema50, ema50 > ema200]
        bear = [price < ema20, ema20 < ema50, ema50 < ema200]

        bull_count = sum(bull)
        bear_count = sum(bear)

        if bull_count == 3:
            htf = "BULL"
        elif bear_count == 3:
            htf = "BEAR"
        else:
            htf = "NEUTRAL"

        # Hard reject ONLY when all 3 signals unanimously oppose direction
        strong_opposing = (
            (direction == "BUY" and bear_count == 3) or
            (direction == "SELL" and bull_count == 3)
        )
        return htf, strong_opposing

    # ──────────────────────────────────────────────────────────
    # Scoring Sub-Methods
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _score_trend(direction: str, htf_trend: str) -> float:
        """Score 0–25. Aligned = 25, neutral = 12, opposing = 5 (not 0 — already filtered)."""
        if direction == "BUY":
            if htf_trend == "BULL":
                return 25.0
            if htf_trend == "NEUTRAL":
                return 12.5
            return 5.0  # weak opposing (2/3 signals)
        # SELL
        if htf_trend == "BEAR":
            return 25.0
        if htf_trend == "NEUTRAL":
            return 12.5
        return 5.0

    @staticmethod
    def _score_momentum(df: pd.DataFrame, direction: str) -> float:
        """
        Score 0–25 on 5m data: RSI zone alignment + EMA slope.

        FIX: previously used 1m df which is too noisy; 5m gives a cleaner
        momentum read without lagging too far behind price action.
        """
        rsi = MarketContext._rsi(df["close"])
        # 20-period EMA slope: positive = uptrend, negative = downtrend
        ema20_series = df["close"].ewm(span=20, adjust=False).mean()
        slope = float(ema20_series.diff().iloc[-3:].mean())  # avg of last 3 bars

        score = 0.0

        if direction == "BUY":
            if 35 <= rsi <= 65:
                score += 12  # RSI in healthy buy zone
            elif rsi < 35:
                score += 18  # oversold — strong buy confirmation
            elif rsi < 75:
                score += 6   # slightly elevated but not overextended
            if slope > 0:
                score += 7

        else:  # SELL
            if 35 <= rsi <= 65:
                score += 12
            elif rsi > 65:
                score += 18  # overbought — strong sell confirmation
            elif rsi > 25:
                score += 6
            if slope < 0:
                score += 7

        return min(score, 25.0)

    @staticmethod
    def _score_volatility(atr: float, atr_avg20: float) -> float:
        """Score 0–15. Prefer ATR within 0.7×–1.5× of 20-period average."""
        ratio = atr / (atr_avg20 + 1e-9)
        if 0.7 <= ratio <= 1.5:
            return 15.0   # ideal — normal, tradeable volatility
        if 1.5 < ratio <= 1.8:
            return 10.0   # elevated but manageable
        if ratio > 1.8:
            return 4.0    # high — risk of slippage
        return 7.0         # low — price may not reach TP

    @staticmethod
    def _score_volume(vol_ratio: float, direction: str, df: pd.DataFrame) -> float:
        """Score 0–15: volume expansion + directional confirmation."""
        score = 0.0
        if vol_ratio >= 2.0:
            score += 10
        elif vol_ratio >= 1.5:
            score += 7
        elif vol_ratio >= 1.0:
            score += 4

        last = df.iloc[-1]
        body = float(last["close"]) - float(last["open"])
        if (direction == "BUY" and body > 0) or (direction == "SELL" and body < 0):
            score += 5

        return min(score, 15.0)
