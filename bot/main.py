"""
main.py  –  KUBERA 2.0
Async orchestrator: wires all 6 modules, manages startup, signal ingestion,
trade lifecycle, and graceful shutdown.

Modes:
    python -m bot.main              → stdin mode (paste signals into terminal)
    python -m bot.main --telegram   → Telegram bot mode (signals via Telegram)
    python -m bot.main --selftest   → evaluate built-in test signals, no orders

FIXES vs v1:
  • Startup: sets leverage per symbol before trading (missing in v1)
  • Startup: reconciles exchange positions (ghost position detection)
  • Startup: validates API keys on init (no crash-on-first-trade surprise)
  • Signal loop: asyncio.Queue with timeout — cleanly cancellable
  • Balance: fetched once at startup + after each trade close
  • Trade close: uses actual exit_price from OpenTrade, not mark price
  • Realised R: computed correctly from actual exit vs SL distance
  • PnL: correctly signed for BUY and SELL trades
  • notify_fn: optional async callback for per-event trade notifications
    (used by TelegramInterface to push updates back to user)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Awaitable, Callable, Optional

from binance import AsyncClient
from binance.exceptions import BinanceAPIException

import bot.config as cfg
from bot.execution import ExitReason, ExecutionEngine, OpenTrade, TradeState
from bot.market_context import MarketContext
from bot.performance_tracker import ClosedTrade, PerformanceTracker
from bot.risk_engine import CapitalStatus, RiskEngine
from bot.signal_engine import RawSignal, SignalDecision, SignalEngine
from bot.signal_parser import SignalParser
from bot.utils import get_logger, sanitise_for_log

logger = get_logger("main")

# Type alias for lifecycle notify callback
NotifyFn = Callable[[str, str], Awaitable[None]]

# ─────────────────────────────────────────────────────────────────────────────
# Self-Test Signals
# ─────────────────────────────────────────────────────────────────────────────

SELF_TEST_RAW = [
    # Should EXECUTE (good RR, structured dict)
    {
        "symbol": "XAUUSDT", "direction": "SELL",
        "entry_low": 5070, "entry_high": 5073, "stop_loss": 5090,
        "take_profits": [5065, 5058, 5045],
    },
    # Should be REJECTED (RR < 1.5 and cannot be optimised)
    {
        "symbol": "BTCUSDT", "direction": "BUY",
        "entry_low": 95000, "entry_high": 95050, "stop_loss": 94900,
        "take_profits": [95100],
    },
    # Telegram-style text signal
    "GOLD SELL @ 4992-4995\nSL 5000\nTP1 4986\nTP2 4974",
]


# ─────────────────────────────────────────────────────────────────────────────
# Main Bot
# ─────────────────────────────────────────────────────────────────────────────

class KuberaBot:
    """
    KUBERA 2.0 — autonomous signal execution engine.
    Initialise → startup → run signal loop → graceful shutdown.
    """

    def __init__(self, selftest: bool = False) -> None:
        self.selftest = selftest
        self._client: Optional[AsyncClient] = None
        self._binance_online: bool = False   # set True only when Binance connect succeeds
        self._risk    = RiskEngine()
        self._balance: float = 0.0
        self._parser  = SignalParser()
        self._active_tasks: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()
        self._signal_queue: asyncio.Queue = asyncio.Queue()
        self._perf: Optional[PerformanceTracker] = None
        self._engine: Optional[SignalEngine] = None
        self._exec: Optional[ExecutionEngine] = None

    # ──────────────────────────────────────────────────────────
    # Startup
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("═" * 60)
        logger.info("  %s  v%s  starting ...", cfg.BOT_NAME, cfg.BOT_VERSION)
        logger.info("  Mode: %s", "PAPER" if cfg.PAPER_TRADING else "LIVE")
        logger.info("  Testnet: %s", cfg.BINANCE_TESTNET)
        logger.info("═" * 60)

        if not cfg.PAPER_TRADING:
            if not cfg.BINANCE_API_KEY or not cfg.BINANCE_SECRET_KEY:
                raise RuntimeError(
                    "BINANCE_API_KEY / BINANCE_SECRET_KEY not set in .env"
                )

        # ── Binance connection (fault-tolerant) ────────────────────────────
        # AsyncClient.create() always pings api.binance.com.  A DNS failure or
        # timeout here would kill the process before Telegram ever starts.
        # We catch all network-level errors and fall back to an offline state so
        # the Telegram bot can still come online and notify the user.
        try:
            self._client = await AsyncClient.create(
                api_key=cfg.BINANCE_API_KEY,
                api_secret=cfg.BINANCE_SECRET_KEY,
                testnet=cfg.BINANCE_TESTNET,
            )
            self._binance_online = True
            logger.info("Binance connection established.")
        except Exception as exc:
            logger.critical(
                "⚠️  Binance connection FAILED at startup: %s\n"
                "    Bot will start in OFFLINE / PAPER mode.\n"
                "    Signals cannot be executed until connectivity is restored.",
                exc,
            )
            # Create a minimal client object without pinging (api_key/secret may
            # be empty — that is fine in paper mode; we just need the object for
            # type checks elsewhere).
            self._client = AsyncClient(
                api_key=cfg.BINANCE_API_KEY or "",
                api_secret=cfg.BINANCE_SECRET_KEY or "",
                testnet=cfg.BINANCE_TESTNET,
            )
            self._binance_online = False
            cfg.PAPER_TRADING = True   # force paper mode when offline

        market = MarketContext(self._client)

        def _on_strict(enabled: bool):
            if self._engine:
                if enabled:
                    self._engine.enable_strict_mode()
                else:
                    self._engine.disable_strict_mode()

        self._perf   = PerformanceTracker(on_strict_mode_change=_on_strict)
        self._engine = SignalEngine(market)
        self._exec   = ExecutionEngine(self._client, market)

        if self._binance_online and not cfg.PAPER_TRADING:
            try:
                await self._client.futures_account()
                logger.info("API credentials validated")
            except BinanceAPIException as exc:
                raise RuntimeError(f"Binance API auth failed: {exc}") from exc

        self._balance = await self._fetch_balance()
        logger.info("Starting balance: %.2f USDT", self._balance)
        await self._risk.update_balance(self._balance)
        self._perf.update_running_balance(self._balance)

        if self._binance_online and not cfg.PAPER_TRADING:
            await self._set_leverage_all()

        if self._binance_online:
            await self._exec.load_exchange_info("XAUUSDT")
            if cfg.STARTUP_RECONCILE and not cfg.PAPER_TRADING:
                ghosts = await self._exec.reconcile_on_startup()
                if ghosts:
                    logger.warning(
                        "%d ghost position(s) recovered — SLs must be set manually!", len(ghosts)
                    )

        status = "ONLINE" if self._binance_online else "OFFLINE (paper-only)"
        logger.info("%s ready. Binance: %s", cfg.BOT_NAME, status)

    async def _set_leverage_all(self) -> None:
        symbols = ["XAUUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
        for sym in symbols:
            try:
                await self._client.futures_change_leverage(
                    symbol=sym, leverage=cfg.LEVERAGE
                )
                logger.info("[%s] Leverage set to %d×", sym, cfg.LEVERAGE)
            except BinanceAPIException as exc:
                logger.warning("[%s] Leverage set failed: %s", sym, exc)

    async def _fetch_balance(self) -> float:
        if cfg.PAPER_TRADING:
            try:
                acct = await self._client.futures_account()
                return float(acct.get("totalWalletBalance", 10000.0))
            except Exception:
                return 10000.0
        try:
            acct = await self._client.futures_account()
            return float(acct["totalWalletBalance"])
        except Exception as exc:
            logger.warning("Balance fetch failed: %s — using %.2f", exc, self._balance)
            return self._balance

    # ──────────────────────────────────────────────────────────
    # Shutdown
    # ──────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        logger.info("Shutting down %s ...", cfg.BOT_NAME)
        self._shutdown.set()
        for task in self._active_tasks:
            if not task.done():
                task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        if self._client:
            await self._client.close_connection()
        if self._perf:
            self._perf.print_summary()
        logger.info("%s shut down cleanly.", cfg.BOT_NAME)

    # ──────────────────────────────────────────────────────────
    # Stdin Signal Loop (non-Telegram mode)
    # ──────────────────────────────────────────────────────────

    async def _stdin_reader(self) -> None:
        """
        Reads stdin line-by-line and assembles multi-line signals into one block.

        Flushing rules (whichever comes first):
          • A blank line (user pressed Enter on an empty line)
          • 1.2 s idle after at least one line has been buffered
          • EOF / shutdown
        """
        loop = asyncio.get_running_loop()
        logger.info("Stdin reader ready. Paste a signal and press Enter.")
        buffer: list[str] = []

        async def _flush():
            nonlocal buffer
            if buffer:
                block = "\n".join(buffer)
                buffer = []
                await self._signal_queue.put(block)

        while not self._shutdown.is_set():
            # Use a shorter timeout once we have lines buffered so we flush quickly
            deadline = 1.2 if buffer else 5.0
            try:
                line = await asyncio.wait_for(
                    loop.run_in_executor(None, sys.stdin.readline),
                    timeout=deadline,
                )
            except asyncio.TimeoutError:
                # Idle timeout — flush whatever is buffered
                await _flush()
                continue
            except Exception as exc:
                logger.error("Stdin read error: %s", exc)
                await _flush()
                break

            line = line.strip()

            # Commands
            if line.lower() in ("quit", "exit", "q"):
                await _flush()
                self._shutdown.set()
                break

            # Blank line = signal separator
            if not line:
                await _flush()
                continue

            buffer.append(line)

    async def _signal_consumer(self) -> None:
        while not self._shutdown.is_set():
            try:
                raw_input = await asyncio.wait_for(
                    self._signal_queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                continue
            task = asyncio.create_task(
                self._handle_signal(raw_input),
                name=f"signal_{int(time.time())}",
            )
            self._active_tasks.append(task)
            task.add_done_callback(
                lambda t: self._active_tasks.remove(t) if t in self._active_tasks else None
            )

    # ──────────────────────────────────────────────────────────
    # Core Signal Pipeline (used by both stdin and Telegram)
    # ──────────────────────────────────────────────────────────

    async def _handle_signal(
        self,
        raw_input: str,
        notify_fn: Optional[NotifyFn] = None,
    ) -> None:
        """Full pipeline: parse → evaluate → size → execute → track."""
        logger.info("─" * 60)
        logger.info("SIGNAL: %s", raw_input[:120])

        # Parse
        try:
            source = json.loads(raw_input)
        except (json.JSONDecodeError, ValueError):
            source = raw_input

        try:
            parsed = self._parser.parse(source)
            raw = RawSignal.from_parsed(parsed)
        except ValueError as exc:
            logger.error("Signal parsing failed: %s", exc)
            return

        await self._exec.load_exchange_info(raw.symbol)

        # Evaluate
        decision: SignalDecision = await self._engine.evaluate(raw)
        if decision.action == "REJECT":
            logger.warning("[%s] REJECTED: %s", raw.symbol, decision.reject_reason)
            print(decision.summary())
            return

        # Risk
        filters = self._exec._get_symbol_filters(raw.symbol)
        result = await self._risk.compute(
            balance=self._balance,
            sl_distance=decision.sl_distance,
            step_size=filters["step_size"],
            reduce_size=decision.reduce_size,
        )
        if not result.allowed:
            logger.warning("[%s] Blocked: %s", raw.symbol, result.reason)
            return

        decision.risk_pct      = result.risk_pct
        decision.position_size = result.position_size
        print(decision.summary())

        # Execute
        trade = await self._exec.execute(decision)
        if trade is None:
            logger.warning("[%s] Trade not entered (timeout/cancelled)", raw.symbol)
            if notify_fn:
                await notify_fn("TIMEOUT", "Zone expired — no entry")
            return

        # Track
        await self._track_until_closed(
            trade, decision, result.risk_pct, notify_fn=notify_fn
        )

    # ──────────────────────────────────────────────────────────
    # Trade Lifecycle Tracking
    # ──────────────────────────────────────────────────────────

    async def _track_until_closed(
        self,
        trade: OpenTrade,
        decision: SignalDecision,
        risk_pct: float,
        notify_fn: Optional[NotifyFn] = None,
    ) -> None:
        """Poll until trade closes, fire notify_fn on key events, record performance."""
        prev_state = trade.state
        sl_notified = False

        while trade.state not in (
            TradeState.CLOSED, TradeState.CANCELLED, TradeState.ERROR
        ):
            await asyncio.sleep(cfg.POSITION_MONITOR_INTERVAL_SEC)

            # TP1 / breakeven event
            if trade.state == TradeState.TP1_HIT and prev_state != TradeState.TP1_HIT:
                if notify_fn:
                    await notify_fn(
                        "TP1_HIT",
                        f"entry={trade.entry_price:.4f}  "
                        f"new_sl={trade.stop_loss:.4f} \\(BE\\)",
                    )
                prev_state = trade.state

            # SL moved to BE notification
            if trade.breakeven_moved and not sl_notified:
                sl_notified = True
                if notify_fn and trade.state != TradeState.TP1_HIT:
                    await notify_fn("BE_MOVED", f"SL moved to {trade.stop_loss:.4f}")

        # Compute result
        exit_price = trade.exit_price or await self._fetch_exit_price(trade)
        sl_dist    = decision.sl_distance
        sign       = 1.0 if trade.direction == "BUY" else -1.0
        realised_r = (sign * (exit_price - trade.entry_price) / sl_dist) if sl_dist > 0 else 0.0
        pnl_usdt   = realised_r * (self._balance * risk_pct / 100.0)

        result_emoji = "✅ WIN" if realised_r > 0 else "❌ LOSS"

        if notify_fn:
            reason_safe = trade.exit_reason.value.replace("_", "\\_")
            await notify_fn(
                "CLOSED",
                f"{result_emoji} R\\={realised_r:.2f}  "
                f"PnL\\={pnl_usdt:+.2f}U  "
                f"exit\\={exit_price:.4f}  "
                f"reason\\={reason_safe}",
            )

        # Record
        closed = ClosedTrade(
            trade_id=f"{trade.symbol}-{int(trade.open_time)}",
            symbol=trade.symbol,
            direction=trade.direction,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            stop_loss=trade.stop_loss,
            take_profits=trade.take_profits,
            position_size=trade.position_size,
            risk_pct=risk_pct,
            blended_rr=decision.blended_rr,
            realised_r=realised_r,
            pnl_usdt=pnl_usdt,
            confidence=decision.confidence,
            exit_reason=trade.exit_reason.value,
            duration_sec=time.time() - trade.open_time,
            optimized=decision.optimization_applied,
            strict_mode=self._engine.strict_mode,
        )
        self._perf.record_trade(closed)

        if realised_r > 0:
            await self._risk.record_win()
        else:
            await self._risk.record_loss()

        self._balance = await self._fetch_balance()
        await self._risk.update_balance(self._balance)
        self._perf.update_running_balance(self._balance)

        logger.info(
            "[%s] %s | R=%.2f PnL=%.2f USDT | balance=%.2f | reason=%s",
            trade.symbol, result_emoji, realised_r, pnl_usdt,
            self._balance, trade.exit_reason.value,
        )
        self._perf.print_summary()

    async def _fetch_exit_price(self, trade: OpenTrade) -> float:
        try:
            resp = await self._client.futures_mark_price(symbol=trade.symbol)
            return float(resp["markPrice"])
        except Exception:
            return trade.stop_loss

    # ──────────────────────────────────────────────────────────
    # Self-Test
    # ──────────────────────────────────────────────────────────

    async def run_selftest(self) -> None:
        logger.info("═" * 60)
        logger.info("  KUBERA 2.0  — SELF-TEST MODE")
        logger.info("═" * 60)
        market = MarketContext(self._client)
        engine = SignalEngine(market)
        for i, raw_input in enumerate(SELF_TEST_RAW, 1):
            logger.info("\n── Test %d ──────────────────────────────", i)
            try:
                if isinstance(raw_input, dict):
                    raw = RawSignal.from_dict(raw_input)
                else:
                    parsed = SignalParser().parse(raw_input)
                    raw = RawSignal.from_parsed(parsed)
            except ValueError as exc:
                logger.error("Test %d parse error: %s", i, exc)
                continue
            try:
                decision = await engine.evaluate(raw)
                print(decision.summary())
            except Exception as exc:
                logger.error("Test %d evaluation error: %s", i, exc)
        logger.info("\nSelf-test complete.")

    # ──────────────────────────────────────────────────────────
    # Run Modes
    # ──────────────────────────────────────────────────────────

    async def run(self, telegram_mode: bool = False) -> None:
        await self.start()

        if self.selftest:
            await self.run_selftest()
            await self.shutdown()
            return

        if telegram_mode:
            from bot.telegram_interface import TelegramInterface
            logger.info("Starting in Telegram mode ...")
            tg = TelegramInterface(self)
            await tg.build_async()   # registers commands with Telegram
            await tg.run_polling()
            await self.shutdown()
            return

        # Stdin mode
        reader_task   = asyncio.create_task(self._stdin_reader(),    name="stdin_reader")
        consumer_task = asyncio.create_task(self._signal_consumer(), name="signal_consumer")
        self._active_tasks.extend([reader_task, consumer_task])
        try:
            await asyncio.gather(reader_task, consumer_task)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{cfg.BOT_NAME} — Autonomous Signal Execution Engine"
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Run in Telegram bot mode (requires TELEGRAM_BOT_TOKEN in .env)",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Evaluate built-in test signals (no real orders)",
    )
    args = parser.parse_args()

    bot = KuberaBot(selftest=args.selftest)

    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop installed")
    except ImportError:
        pass

    try:
        asyncio.run(bot.run(telegram_mode=args.telegram))
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
    except Exception as exc:
        logger.critical("Fatal error: %s", sanitise_for_log(str(exc)), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
