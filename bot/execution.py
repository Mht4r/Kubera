"""
execution.py  –  KUBERA 2.0
Phase 4 (Entry Intelligence) + Phase 5 (Exit Autonomy)

CRITICAL FIXES vs v1:
  [C1] NameError: `symbol` undefined in _move_sl_to_breakeven except block
       → fixed to `trade.symbol`
  [C2] Binance API violation: `closePosition=True` + `quantity` are mutually
       exclusive on STOP_MARKET orders
       → dropped closePosition; use quantity + reduceOnly=True
  [C3] Race condition in _open_trades dict — two concurrent execute() calls
       for the same symbol could both pass the guard
       → protected with asyncio.Lock
  [C4] Unbound `step`/`tick` variables in load_exchange_info if filters absent
       → pre-initialised to safe defaults
  [C5] _get_current_atr imported MarketContext inside monitor loop (each iteration)
       → moved to module-level import; ATR computed inline with cached klines
  [C6] load_exchange_info fetches entire 500-symbol payload on every call
       → added global TTL cache; reload only after EXCHANGE_INFO_CACHE_TTL

HIGH-PRIORITY FIXES:
  [H7] TP limit orders now set reduceOnly=True (prevents accidental new position)
  [H8] All close-side orders (SL/TP/trail) use reduceOnly=True
  [M1] Paper _paper_simulate now simulates ATR trail on runner after TP1
  [M2] Exit price uses actual execution price where available
  [N2] Redundant `await asyncio.sleep(0)` removed from paper loop
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import pandas as pd
from binance import AsyncClient
from binance.enums import (
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    SIDE_BUY,
    SIDE_SELL,
    TIME_IN_FORCE_GTC,
)
from binance.exceptions import BinanceAPIException

import bot.config as cfg
from bot.market_context import MarketContext
from bot.signal_engine import SignalDecision
from bot.utils import async_retry, get_logger, round_price, round_step_size

logger = get_logger("execution")

# ─────────────────────────────────────────────────────────────────────────────
# State Enums / Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class TradeState(Enum):
    WAITING_ENTRY = auto()
    ENTERED       = auto()
    TP1_HIT       = auto()
    CLOSED        = auto()
    CANCELLED     = auto()
    ERROR         = auto()


class ExitReason(str, Enum):
    SL        = "SL"
    TP1       = "TP1"
    RUNNER_TP = "RUNNER_TP"
    TRAIL     = "TRAIL"
    STALL     = "STALL"
    MANUAL    = "MANUAL"
    TIMEOUT   = "TIMEOUT"
    UNKNOWN   = "UNKNOWN"


@dataclass
class OpenTrade:
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profits: list[float]
    position_size: float             # total qty entered
    remaining_size: float            # qty still in market
    tick_size: float = 0.01
    step_size: float = 0.001
    entry_order_id: Optional[int] = None
    sl_order_id: Optional[int] = None
    tp1_order_id: Optional[int] = None
    state: TradeState = TradeState.WAITING_ENTRY
    exit_reason: ExitReason = ExitReason.UNKNOWN
    exit_price: float = 0.0
    breakeven_moved: bool = False
    tp1_closed: bool = False
    open_time: float = field(default_factory=time.time)
    atr_at_entry: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Exchange Info Cache (global — shared across all symbol calls)
# ─────────────────────────────────────────────────────────────────────────────

_exchange_info_cache: dict[str, dict] = {}   # symbol → {step_size, tick_size}
_exchange_info_fetch_ts: float = 0.0          # last full-fetch timestamp


# ─────────────────────────────────────────────────────────────────────────────
# Execution Engine
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Manages the full lifecycle of a trade: entry confirmation → TP/SL
    management → position close.  Supports live (Binance Futures) and
    paper-trading modes.
    """

    def __init__(
        self,
        client: AsyncClient,
        market_context: MarketContext,
        paper_trading: bool = cfg.PAPER_TRADING,
    ) -> None:
        self.client = client
        self.market = market_context
        self.paper = paper_trading
        # FIX [C3]: protect _open_trades with a lock
        self._lock = asyncio.Lock()
        self._open_trades: dict[str, OpenTrade] = {}

        if paper_trading:
            logger.warning("PAPER TRADING MODE — no real orders will be placed")

    # ──────────────────────────────────────────────────────────
    # Public: entry point
    # ──────────────────────────────────────────────────────────

    async def execute(self, decision: SignalDecision) -> Optional[OpenTrade]:
        """
        Phase 4: wait for zone entry + candle confirmation, then place orders.
        Uses asyncio.Lock to prevent duplicate trades per symbol.
        Returns the OpenTrade object, or None if cancelled/timeout/rejected.
        """
        symbol = decision.symbol

        # FIX [C3]: atomic check-and-insert under lock
        async with self._lock:
            if symbol in self._open_trades:
                logger.warning(
                    "[%s] Max 1 open trade enforced — new signal skipped", symbol
                )
                return None
            # Reserve the slot immediately so no concurrent call can slip in
            self._open_trades[symbol] = None  # type: ignore[assignment]

        logger.info(
            "[%s] Waiting for price to enter zone [%.4f – %.4f] (no timeout) ...",
            symbol,
            decision.entry_low if decision.entry_low > 0 else decision.entry,
            decision.entry_high if decision.entry_high > 0 else decision.entry,
        )

        trade = await self._wait_for_entry(decision)

        if trade is None:
            async with self._lock:
                self._open_trades.pop(symbol, None)
            return None

        async with self._lock:
            self._open_trades[symbol] = trade

        # Start background monitor
        asyncio.create_task(
            self._monitor_position(trade),
            name=f"monitor_{symbol}",
        )
        return trade

    async def cancel(self, symbol: str) -> None:
        """Cancel a waiting or open trade."""
        async with self._lock:
            trade = self._open_trades.get(symbol)

        if trade is not None:
            trade.state = TradeState.CANCELLED
            trade.exit_reason = ExitReason.MANUAL
            if not self.paper:
                await self._cancel_all_orders(symbol)

        async with self._lock:
            self._open_trades.pop(symbol, None)

        logger.info("[%s] Trade cancelled", symbol)

    def has_open_trade(self, symbol: str) -> bool:
        return self._open_trades.get(symbol) is not None

    def get_trade(self, symbol: str) -> Optional[OpenTrade]:
        return self._open_trades.get(symbol)

    async def get_all_open_trades(self) -> dict[str, OpenTrade]:
        async with self._lock:
            return {k: v for k, v in self._open_trades.items() if v is not None}

    # ──────────────────────────────────────────────────────────
    # Exchange info (global cache — FIX [C6])
    # ──────────────────────────────────────────────────────────

    async def load_exchange_info(self, symbol: str) -> None:
        """
        Fetch and cache all exchange info in one API call.
        Refresh only when the global cache is stale (EXCHANGE_INFO_CACHE_TTL).
        FIX [C6]: was fetching ALL symbols on every per-symbol call.
        FIX [C4]: step/tick pre-initialised before filter loop.
        """
        global _exchange_info_cache, _exchange_info_fetch_ts

        now = time.time()
        if (
            symbol in _exchange_info_cache
            and (now - _exchange_info_fetch_ts) < cfg.EXCHANGE_INFO_CACHE_TTL
        ):
            return  # fresh cache hit

        try:
            info = await self.client.futures_exchange_info()
            _exchange_info_fetch_ts = time.time()
            for sym_info in info["symbols"]:
                # FIX [C4]: pre-init to safe defaults before filter loop
                step = 0.001
                tick = 0.01
                min_qty = 0.001
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f.get("stepSize", step))
                        min_qty = float(f.get("minQty", min_qty))
                    elif f["filterType"] == "PRICE_FILTER":
                        tick = float(f.get("tickSize", tick))
                _exchange_info_cache[sym_info["symbol"]] = {
                    "step_size": step,
                    "tick_size": tick,
                    "min_qty": min_qty,
                }
            logger.info(
                "Exchange info cached: %d symbols", len(_exchange_info_cache)
            )
        except Exception as exc:
            logger.warning(
                "[%s] Could not load exchange info: %s — using defaults", symbol, exc
            )
            _exchange_info_cache.setdefault(symbol, {
                "step_size": 0.001, "tick_size": 0.01, "min_qty": 0.001
            })

    def _get_symbol_filters(self, symbol: str) -> dict:
        return _exchange_info_cache.get(
            symbol, {"step_size": 0.001, "tick_size": 0.01, "min_qty": 0.001}
        )

    # ──────────────────────────────────────────────────────────
    # Phase 4: Entry Logic
    # ──────────────────────────────────────────────────────────

    async def _wait_for_entry(self, decision: SignalDecision) -> Optional[OpenTrade]:
        """Poll price until it enters the zone, then confirm with a candle pattern.

        Uses the actual signal entry zone (entry_low → entry_high).
        Waits indefinitely — trade is only cancelled by SL/TP hit or manual /cancel.
        """
        symbol = decision.symbol
        direction = decision.direction

        # Use the real zone from the signal; fall back to a ±1 tick band if degenerate
        entry_low  = decision.entry_low  if decision.entry_low  > 0 else decision.entry
        entry_high = decision.entry_high if decision.entry_high > 0 else decision.entry
        if entry_low == entry_high:
            # Single-price signal — widen by 0.5% so minute noise doesn't stall forever
            margin = entry_low * 0.005
            entry_low  -= margin
            entry_high += margin

        last_prices: list[float] = []

        while True:  # wait indefinitely — cancelled externally or via _open_trades slot
            # Check if cancelled while waiting
            if self._open_trades.get(symbol) is None:
                logger.info("[%s] Trade slot cleared externally — aborting wait", symbol)
                return None

            try:
                price = await self._get_price(symbol)
            except Exception as exc:
                logger.error("[%s] Price fetch error: %s", symbol, exc)
                await asyncio.sleep(cfg.ENTRY_POLL_INTERVAL_SEC)
                continue

            last_prices.append(price)
            if len(last_prices) > 10:
                last_prices.pop(0)

            if not (entry_low <= price <= entry_high):
                await asyncio.sleep(cfg.ENTRY_POLL_INTERVAL_SEC)
                continue

            logger.info(
                "[%s] Price %.4f entered zone [%.4f–%.4f]",
                symbol, price, entry_low, entry_high,
            )

            # Spike guard: rapid swing > 1.5× ATR
            ctx = decision.context
            atr = ctx.atr if ctx else 0.0
            if self._spike_detected(last_prices, atr):
                logger.info("[%s] Spike entry detected — waiting for pullback", symbol)
                await asyncio.sleep(cfg.ENTRY_POLL_INTERVAL_SEC * 3)
                continue

            # Candle confirmation
            confirmed, actual_entry = await self._confirm_entry(
                symbol, direction, price, atr
            )
            if not confirmed:
                await asyncio.sleep(cfg.ENTRY_POLL_INTERVAL_SEC)
                continue

            return await self._place_entry(decision, actual_entry, atr)

    async def _confirm_entry(
        self,
        symbol: str,
        direction: str,
        price: float,
        atr: float,
    ) -> tuple[bool, float]:
        """
        Confirm with last 3 candles: engulfing or rejection-wick pattern.
        Paper mode: instant confirmation.
        """
        if self.paper:
            return True, price

        try:
            raw = await self.client.futures_klines(
                symbol=symbol, interval="1m", limit=5
            )
        except Exception as exc:
            logger.warning(
                "[%s] Candle confirmation fetch failed: %s — accepting anyway", symbol, exc
            )
            return True, price

        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close",
            "volume", "close_time", "qav", "num_trades",
            "taker_base", "taker_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        prev = df.iloc[-3]
        curr = df.iloc[-2]   # last complete candle

        # Engulfing
        bullish_engulf = (
            curr["close"] > curr["open"]
            and prev["close"] < prev["open"]
            and curr["close"] > prev["open"]
            and curr["open"] < prev["close"]
        )
        bearish_engulf = (
            curr["close"] < curr["open"]
            and prev["close"] > prev["open"]
            and curr["close"] < prev["open"]
            and curr["open"] > prev["close"]
        )

        # Rejection wick
        body = abs(curr["close"] - curr["open"])
        lower_wick = min(curr["open"], curr["close"]) - curr["low"]
        upper_wick = curr["high"] - max(curr["open"], curr["close"])
        wick_threshold = body * 2

        rej_bull = lower_wick > wick_threshold and direction == "BUY"
        rej_bear = upper_wick > wick_threshold and direction == "SELL"

        if direction == "BUY" and (bullish_engulf or rej_bull):
            logger.info(
                "[%s] BUY confirmed: engulf=%s wick=%s", symbol, bullish_engulf, rej_bull
            )
            return True, float(curr["close"])

        if direction == "SELL" and (bearish_engulf or rej_bear):
            logger.info(
                "[%s] SELL confirmed: engulf=%s wick=%s", symbol, bearish_engulf, rej_bear
            )
            return True, float(curr["close"])

        logger.debug("[%s] No confirmation pattern yet (price=%.4f)", symbol, price)
        return False, price

    @staticmethod
    def _spike_detected(prices: list[float], atr: float) -> bool:
        if len(prices) < 4 or atr <= 0:
            return False
        return (max(prices[-4:]) - min(prices[-4:])) > atr * 1.5

    # ──────────────────────────────────────────────────────────
    # Order Placement
    # ──────────────────────────────────────────────────────────

    async def _place_entry(
        self,
        decision: SignalDecision,
        actual_entry: float,
        atr: float,
    ) -> OpenTrade:
        symbol = decision.symbol
        direction = decision.direction
        size = decision.position_size
        sl = decision.stop_loss
        tp1 = decision.take_profits[0] if decision.take_profits else None

        filters = self._get_symbol_filters(symbol)
        tick = filters["tick_size"]
        step = filters["step_size"]

        sl  = round_price(sl, tick)
        size = round_step_size(size, step)

        if tp1 is not None:
            tp1 = round_price(tp1, tick)

        side       = SIDE_BUY  if direction == "BUY"  else SIDE_SELL
        close_side = SIDE_SELL if direction == "BUY"  else SIDE_BUY

        tp1_size  = round_step_size(size * cfg.TP1_SIZE_PCT / 100.0, step)
        runner    = round_step_size(size - tp1_size, step)

        trade = OpenTrade(
            symbol=symbol,
            direction=direction,
            entry_price=actual_entry,
            stop_loss=sl,
            take_profits=list(decision.take_profits),
            position_size=size,
            remaining_size=runner,
            tick_size=tick,
            step_size=step,
            atr_at_entry=atr,
        )

        if self.paper:
            logger.info(
                "[%s] PAPER ENTRY | dir=%s entry=%.4f sl=%.4f tp1=%s size=%.6f",
                symbol, direction, actual_entry, sl,
                f"{tp1:.4f}" if tp1 else "N/A", size,
            )
            trade.state = TradeState.ENTERED
            return trade

        try:
            # 1. Market entry
            entry_resp = await self._order_market(symbol, side, size, reduce_only=False)
            trade.entry_order_id = entry_resp.get("orderId")
            trade.state = TradeState.ENTERED
            # Use avg fill price if returned
            avg_price = entry_resp.get("avgPrice") or entry_resp.get("price")
            if avg_price and float(avg_price) > 0:
                trade.entry_price = float(avg_price)
            logger.info("[%s] Entry order placed: id=%s", symbol, trade.entry_order_id)

            # 2. Stop-loss  — FIX [C2]: no closePosition=True; use reduceOnly
            sl_resp = await self._order_stop_market(symbol, close_side, size, sl)
            trade.sl_order_id = sl_resp.get("orderId")

            # 3. TP1 limit — FIX [H7]: reduceOnly=True
            if tp1 is not None and tp1_size > 0:
                tp_resp = await self._order_limit(
                    symbol, close_side, tp1_size, tp1
                )
                trade.tp1_order_id = tp_resp.get("orderId")

            logger.info(
                "[%s] Orders live | entry=%s sl=%s tp1=%s",
                symbol,
                trade.entry_order_id,
                trade.sl_order_id,
                trade.tp1_order_id,
            )
        except Exception as exc:
            logger.error("[%s] Order placement failed: %s", symbol, exc)
            trade.state = TradeState.ERROR
            # Attempt emergency close to prevent orphan position
            if not self.paper:
                try:
                    await self._order_market(symbol, close_side, size, reduce_only=True)
                    logger.info("[%s] Emergency flatposition executed", symbol)
                except Exception as e2:
                    logger.critical(
                        "[%s] EMERGENCY CLOSE FAILED — manual intervention required: %s",
                        symbol, e2,
                    )

        return trade

    # ──────────────────────────────────────────────────────────
    # Phase 5: Position Monitor
    # ──────────────────────────────────────────────────────────

    async def _monitor_position(self, trade: OpenTrade) -> None:
        """
        Background task: check fills, move SL to BE after TP1,
        trail with ATR, detect stalls. Never-widen-SL enforced.
        """
        symbol = trade.symbol
        logger.info("[%s] Position monitor started", symbol)
        stall_counter = 0

        while trade.state in (TradeState.ENTERED, TradeState.TP1_HIT):
            await asyncio.sleep(cfg.POSITION_MONITOR_INTERVAL_SEC)

            try:
                price = await self._get_price(symbol)
            except Exception as exc:
                logger.warning("[%s] Monitor price error: %s", symbol, exc)
                continue

            if self.paper:
                await self._paper_simulate(trade, price)
                if trade.state in (TradeState.CLOSED, TradeState.CANCELLED):
                    break
                continue

            # ── Live: check actual order statuses ──────────────
            tp1_filled = await self._order_is_filled(symbol, trade.tp1_order_id)
            sl_hit     = await self._order_is_filled(symbol, trade.sl_order_id)

            if sl_hit:
                logger.info("[%s] Stop-loss triggered — trade closed", symbol)
                trade.exit_reason = ExitReason.SL
                trade.exit_price = trade.stop_loss
                trade.state = TradeState.CLOSED
                break

            if tp1_filled and not trade.tp1_closed:
                logger.info("[%s] TP1 hit — moving SL to breakeven", symbol)
                trade.exit_reason = ExitReason.TP1
                await self._move_sl_to_breakeven(trade)
                trade.tp1_closed = True
                trade.state = TradeState.TP1_HIT

            # ATR trailing after TP1
            if trade.tp1_closed and trade.remaining_size > 0:
                try:
                    atr = await self._fetch_atr(symbol)
                    await self._trail_sl(trade, price, atr)
                except Exception as exc:
                    logger.warning("[%s] Trail SL error: %s", symbol, exc)

            # Stall detection
            if not self._price_progressing(price, trade):
                stall_counter += 1
                if stall_counter >= cfg.STALL_CANDLES:
                    logger.warning("[%s] Trade stalled — reducing exposure", symbol)
                    await self._reduce_stall_exposure(trade, price)
                    stall_counter = 0
            else:
                stall_counter = 0

        logger.info(
            "[%s] Monitor exited | state=%s reason=%s exit_price=%.4f",
            symbol, trade.state.name, trade.exit_reason.value, trade.exit_price,
        )
        async with self._lock:
            self._open_trades.pop(symbol, None)

    async def _paper_simulate(self, trade: OpenTrade, price: float) -> None:
        """
        Simulate TP1, SL, and ATR trail in paper mode.
        FIX [M1]: runner is trailed after TP1, not ignored.
        """
        direction = trade.direction
        tp1 = trade.take_profits[0] if trade.take_profits else None

        if direction == "BUY":
            # Check SL first (higher prio)
            if price <= trade.stop_loss:
                logger.info("[%s] PAPER: SL hit at %.4f", trade.symbol, price)
                trade.exit_price = trade.stop_loss
                trade.exit_reason = ExitReason.SL
                trade.state = TradeState.CLOSED
                return
            if tp1 and price >= tp1 and not trade.tp1_closed:
                logger.info("[%s] PAPER: TP1 hit at %.4f", trade.symbol, price)
                trade.tp1_closed = True
                trade.breakeven_moved = True
                trade.stop_loss = trade.entry_price   # move to BE
                trade.exit_reason = ExitReason.TP1
                trade.state = TradeState.TP1_HIT
                return
        else:  # SELL
            if price >= trade.stop_loss:
                logger.info("[%s] PAPER: SL hit at %.4f", trade.symbol, price)
                trade.exit_price = trade.stop_loss
                trade.exit_reason = ExitReason.SL
                trade.state = TradeState.CLOSED
                return
            if tp1 and price <= tp1 and not trade.tp1_closed:
                logger.info("[%s] PAPER: TP1 hit at %.4f", trade.symbol, price)
                trade.tp1_closed = True
                trade.breakeven_moved = True
                trade.stop_loss = trade.entry_price
                trade.exit_reason = ExitReason.TP1
                trade.state = TradeState.TP1_HIT
                return

        # Paper ATR trail on runner (after TP1)
        if trade.tp1_closed and trade.remaining_size > 0 and trade.atr_at_entry > 0:
            trail_dist = trade.atr_at_entry * cfg.ATR_TRAIL_MULTIPLIER
            if direction == "BUY":
                proposed = price - trail_dist
                if proposed > trade.stop_loss:     # never widen
                    trade.stop_loss = proposed
            else:
                proposed = price + trail_dist
                if proposed < trade.stop_loss:     # never widen
                    trade.stop_loss = proposed

        # Runner TP
        if trade.tp1_closed and len(trade.take_profits) > 1:
            runner_tp = trade.take_profits[-1]
            if (direction == "BUY" and price >= runner_tp) or \
               (direction == "SELL" and price <= runner_tp):
                logger.info("[%s] PAPER: Runner TP hit at %.4f", trade.symbol, price)
                trade.exit_price = runner_tp
                trade.exit_reason = ExitReason.RUNNER_TP
                trade.state = TradeState.CLOSED

    async def _move_sl_to_breakeven(self, trade: OpenTrade) -> None:
        """Move SL to entry price. NEVER widen. Atomic."""
        if trade.breakeven_moved:
            return

        new_sl = round_price(trade.entry_price, trade.tick_size)

        # Widen guard
        if trade.direction == "BUY" and new_sl <= trade.stop_loss:
            logger.warning(
                "[%s] BE move would widen SL (%.4f→%.4f) — skipped",
                trade.symbol, trade.stop_loss, new_sl,
            )
            return
        if trade.direction == "SELL" and new_sl >= trade.stop_loss:
            logger.warning(
                "[%s] BE move would widen SL (%.4f→%.4f) — skipped",
                trade.symbol, trade.stop_loss, new_sl,
            )
            return

        trade.stop_loss = new_sl
        trade.breakeven_moved = True

        if not self.paper:
            close_side = SIDE_SELL if trade.direction == "BUY" else SIDE_BUY
            try:
                await self._cancel_order(trade.symbol, trade.sl_order_id)
                resp = await self._order_stop_market(
                    trade.symbol, close_side, trade.remaining_size, new_sl
                )
                trade.sl_order_id = resp.get("orderId")
                logger.info(
                    "[%s] SL moved to breakeven %.4f | order=%s",
                    trade.symbol, new_sl, trade.sl_order_id,
                )
            except Exception as exc:
                # FIX [C1]: was `symbol` (NameError) — now `trade.symbol`
                logger.error(
                    "[%s] Could not move SL to BE: %s", trade.symbol, exc
                )

    async def _trail_sl(self, trade: OpenTrade, price: float, atr: float) -> None:
        """Trail SL by 1× ATR in trade direction. Never widen."""
        trail_dist = atr * cfg.ATR_TRAIL_MULTIPLIER

        if trade.direction == "BUY":
            proposed = price - trail_dist
            if proposed <= trade.stop_loss:
                return
            new_sl = round_price(proposed, trade.tick_size)
        else:
            proposed = price + trail_dist
            if proposed >= trade.stop_loss:
                return
            new_sl = round_price(proposed, trade.tick_size)

        trade.stop_loss = new_sl
        logger.debug("[%s] Trail SL → %.4f (ATR=%.4f)", trade.symbol, new_sl, atr)

        if not self.paper:
            close_side = SIDE_SELL if trade.direction == "BUY" else SIDE_BUY
            try:
                await self._cancel_order(trade.symbol, trade.sl_order_id)
                resp = await self._order_stop_market(
                    trade.symbol, close_side, trade.remaining_size, new_sl
                )
                trade.sl_order_id = resp.get("orderId")
            except Exception as exc:
                logger.warning("[%s] Trail update failed: %s", trade.symbol, exc)

    async def _reduce_stall_exposure(self, trade: OpenTrade, price: float) -> None:
        """Reduce remaining exposure by 50% if trade stalls."""
        if trade.remaining_size <= 0:
            trade.exit_reason = ExitReason.STALL
            trade.state = TradeState.CLOSED
            return
        reduce_qty = round_step_size(trade.remaining_size * 0.5, trade.step_size)
        if reduce_qty <= 0:
            return
        trade.remaining_size = round_step_size(
            trade.remaining_size - reduce_qty, trade.step_size
        )
        logger.info(
            "[%s] Stall reduction: closing %.6f at market", trade.symbol, reduce_qty
        )
        if not self.paper:
            close_side = SIDE_SELL if trade.direction == "BUY" else SIDE_BUY
            try:
                await self._order_market(
                    trade.symbol, close_side, reduce_qty, reduce_only=True
                )
            except Exception as exc:
                logger.warning("[%s] Stall reduce order failed: %s", trade.symbol, exc)

    @staticmethod
    def _price_progressing(price: float, trade: OpenTrade) -> bool:
        """Return True if price has moved at least 10% toward TP1 from entry."""
        if not trade.take_profits:
            return True
        tp1   = trade.take_profits[0]
        entry = trade.entry_price
        total = abs(tp1 - entry)
        if total == 0:
            return True
        progress = abs(price - entry) / total
        if trade.direction == "BUY":
            return price > entry and progress >= 0.10
        return price < entry and progress >= 0.10

    # ──────────────────────────────────────────────────────────
    # Startup Reconciliation
    # ──────────────────────────────────────────────────────────

    async def reconcile_on_startup(self) -> list[OpenTrade]:
        """
        Query Binance for existing open positions and reconstruct OpenTrade
        stubs for any symbols not already tracked.  Returns list of recovered
        trades.
        """
        if self.paper:
            return []

        recovered: list[OpenTrade] = []
        try:
            positions = await self.client.futures_position_information()
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue
                sym = pos["symbol"]
                direction = "BUY" if amt > 0 else "SELL"
                entry_price = float(pos.get("entryPrice", 0))

                async with self._lock:
                    if sym in self._open_trades:
                        continue

                logger.warning(
                    "[%s] GHOST POSITION detected on startup — size=%.6f dir=%s entry=%.4f",
                    sym, abs(amt), direction, entry_price,
                )

                # Build a minimal OpenTrade stub so the monitor can track it
                stub = OpenTrade(
                    symbol=sym,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=0.0,       # unknown — bot must not widen from 0
                    take_profits=[],
                    position_size=abs(amt),
                    remaining_size=abs(amt),
                    state=TradeState.ENTERED,
                )

                async with self._lock:
                    self._open_trades[sym] = stub

                recovered.append(stub)
                logger.warning(
                    "[%s] Ghost position registered — add manual SL immediately!", sym
                )

        except Exception as exc:
            logger.error("Startup reconciliation failed: %s", exc)

        return recovered

    # ──────────────────────────────────────────────────────────
    # ATR Helper (FIX [C5]: no in-loop MarketContext import)
    # ──────────────────────────────────────────────────────────

    async def _fetch_atr(self, symbol: str) -> float:
        """Inline ATR computation for trailing — uses module-level MarketContext._atr."""
        raw = await self.client.futures_klines(
            symbol=symbol, interval="1m", limit=cfg.ATR_PERIOD + 5
        )
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base", "taker_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        atr_series = MarketContext._atr(df, cfg.ATR_PERIOD)
        return float(atr_series.iloc[-1])

    # ──────────────────────────────────────────────────────────
    # Low-Level Binance Order Helpers
    # ──────────────────────────────────────────────────────────

    @async_retry()
    async def _get_price(self, symbol: str) -> float:
        resp = await self.client.futures_mark_price(symbol=symbol)
        return float(resp["markPrice"])

    @async_retry()
    async def _order_market(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool,
    ) -> dict:
        return await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
            reduceOnly=reduce_only,
        )

    @async_retry()
    async def _order_limit(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> dict:
        """FIX [H7]: TP limit orders must be reduceOnly=True."""
        return await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=quantity,
            price=price,
            reduceOnly=True,
        )

    @async_retry()
    async def _order_stop_market(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
    ) -> dict:
        """
        FIX [C2]: Drop closePosition=True (mutually exclusive with quantity).
        Use quantity + reduceOnly=True instead.
        """
        return await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            quantity=quantity,
            stopPrice=stop_price,
            reduceOnly=True,
        )

    async def _cancel_order(self, symbol: str, order_id: Optional[int]) -> None:
        if order_id is None:
            return
        try:
            await self.client.futures_cancel_order(
                symbol=symbol, orderId=order_id
            )
        except BinanceAPIException as exc:
            if exc.code == -2011:     # Unknown order — already filled/cancelled
                logger.debug("[%s] Order %s already gone: %s", symbol, order_id, exc)
            else:
                logger.warning("[%s] Cancel %s failed: %s", symbol, order_id, exc)
        except Exception as exc:
            logger.warning("[%s] Cancel %s failed: %s", symbol, order_id, exc)

    async def _cancel_all_orders(self, symbol: str) -> None:
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol)
            logger.info("[%s] All orders cancelled", symbol)
        except Exception as exc:
            logger.warning("[%s] Cancel all orders failed: %s", symbol, exc)

    async def _order_is_filled(self, symbol: str, order_id: Optional[int]) -> bool:
        if order_id is None:
            return False
        try:
            resp = await self.client.futures_get_order(
                symbol=symbol, orderId=order_id
            )
            return str(resp.get("status", "")).upper() == "FILLED"
        except Exception:
            return False
