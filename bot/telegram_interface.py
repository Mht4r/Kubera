"""
telegram_interface.py  –  KUBERA 2.0
Telegram bot frontend: receives signals via chat message, runs the full
6-phase pipeline, and sends all trade lifecycle updates back to the user.

Usage:
    python -m bot.main --telegram

Required env vars:
    TELEGRAM_BOT_TOKEN       — from @BotFather
    TELEGRAM_ALLOWED_USERS   — comma-separated Telegram user IDs (whitelist)

Security model:
    Every handler silently ignores any user ID not in TELEGRAM_ALLOWED_USERS.
    Unknown users receive no response — the bot does not reveal it exists.

Supported commands:
    /help              — show usage guide
    /status            — list all open trades
    /cancel SYMBOL     — cancel waiting/open trade for symbol
    /stats             — rolling performance summary
    /balance           — current Binance futures wallet balance
    /paper             — toggle paper-trading mode
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import bot.config as cfg
from bot.execution import TradeState
from bot.signal_engine import RawSignal, SignalDecision
from bot.signal_parser import SignalParser
from bot.utils import get_logger

if TYPE_CHECKING:
    from bot.main import KuberaBot

logger = get_logger("telegram")

_parser = SignalParser()

# ─────────────────────────────────────────────────────────────────────────────
# Auth guard
# ─────────────────────────────────────────────────────────────────────────────

def _authorised(update: Update) -> bool:
    """Return True if the sending user is on the whitelist."""
    uid = update.effective_user.id if update.effective_user else None
    if uid is None or uid not in cfg.TELEGRAM_ALLOWED_USERS:
        logger.warning("Unauthorised Telegram access from user_id=%s", uid)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Message Formatters
# ─────────────────────────────────────────────────────────────────────────────

_DECISION_ICONS = {"EXECUTE": "🟢", "OPTIMIZE": "🔵", "REJECT": "🔴"}


def _fmt_decision(d: SignalDecision) -> str:
    icon = _DECISION_ICONS.get(d.action, "⚪")
    lines = [
        f"{icon} *KUBERA 2\\.0 — {d.action}*",
        f"```",
        f"Symbol    : {d.symbol}",
        f"Direction : {d.direction}",
        f"Entry     : {d.entry:.4f}" + (" [OPTIMIZED]" if d.optimization_applied else ""),
        f"Stop Loss : {d.stop_loss:.4f}",
        f"Take Prft : {', '.join(f'{t:.4f}' for t in d.take_profits)}",
        f"SL Dist   : {d.sl_distance:.4f}",
        f"Blended RR: 1:{d.blended_rr:.2f}",
        f"Confidence: {d.confidence:.0f}/100",
        f"Risk %    : {d.risk_pct:.2f}%",
        f"Size      : {d.position_size:.6f}",
    ]
    if d.reject_reason:
        lines.append(f"Reason    : {d.reject_reason}")
    if d.justification:
        lines.append(f"Detail    : {d.justification}")
    lines.append("```")
    return "\n".join(lines)


def _fmt_stats(stats) -> str:
    return (
        "📊 *KUBERA 2\\.0 — Performance*\n"
        "```\n"
        f"Total Trades  : {stats.total_trades}\n"
        f"Win Rate      : {stats.win_rate:.1f}% ({stats.wins}W / {stats.losses}L)\n"
        f"Profit Factor : {stats.profit_factor:.2f}\n"
        f"Avg R         : {stats.avg_r:.2f}R\n"
        f"Expectancy    : {stats.expectancy_r:.2f}R\n"
        f"Max Drawdown  : {stats.max_drawdown_pct:.2f}%\n"
        f"Strict Mode   : {'ON ⚠️' if stats.strict_mode_active else 'OFF'}\n"
        "```"
    )


def _fmt_trade_update(symbol: str, event: str, detail: str) -> str:
    icons = {
        "ENTERED"  : "📥",
        "TP1_HIT"  : "🎯",
        "SL_HIT"   : "🛑",
        "TRAIL"    : "📈",
        "BE_MOVED" : "⚖️",
        "CLOSED"   : "✅",
        "TIMEOUT"  : "⏰",
        "STALL"    : "⚠️",
        "PAPER"    : "📄",
    }
    icon = icons.get(event, "ℹ️")
    safe_detail = detail.replace(".", "\\.").replace("-", "\\-").replace("(", "\\(").replace(")", "\\)")
    safe_sym = symbol.replace("_", "\\_")
    return f"{icon} *\\[{safe_sym}\\]* {event}: {safe_detail}"


# ─────────────────────────────────────────────────────────────────────────────
# Telegram Interface Class
# ─────────────────────────────────────────────────────────────────────────────

class TelegramInterface:
    """
    Wires the python-telegram-bot Application to the KuberaBot core.
    All trade lifecycle notifications are pushed back to the originating chat.
    """

    def __init__(self, kubera: "KuberaBot") -> None:
        self.kubera = kubera
        self._app: Optional[Application] = None
        # Map symbol → chat_id so we can notify the correct conversation
        self._trade_chats: dict[str, int] = {}

    # ──────────────────────────────────────────────────────────
    # Build & Run
    # ──────────────────────────────────────────────────────────

    def build(self) -> "TelegramInterface":
        """Construct the Application and register all handlers."""
        if not cfg.TELEGRAM_BOT_TOKEN:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN not set in .env — cannot start Telegram bot"
            )

        self._app = (
            Application.builder()
            .token(cfg.TELEGRAM_BOT_TOKEN)
            .build()
        )

        app = self._app

        # Commands
        app.add_handler(CommandHandler("start",   self._cmd_help))
        app.add_handler(CommandHandler("help",    self._cmd_help))
        app.add_handler(CommandHandler("status",  self._cmd_status))
        app.add_handler(CommandHandler("cancel",  self._cmd_cancel))
        app.add_handler(CommandHandler("stats",   self._cmd_stats))
        app.add_handler(CommandHandler("balance", self._cmd_balance))
        app.add_handler(CommandHandler("paper",   self._cmd_paper))

        # Any free-text message → signal
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_signal)
        )

        logger.info("Telegram Application built — token set, handlers registered")
        return self

    async def run_polling(self) -> None:
        """Start polling in the existing event loop (called from main.py)."""
        if self._app is None:
            raise RuntimeError("Call build() before run_polling()")

        logger.info("Starting Telegram polling ...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,    # ignore queued messages from when bot was offline
        )
        logger.info("Telegram bot is live. Waiting for signals ...")

        # Run until shutdown event is set
        await self.kubera._shutdown.wait()

        logger.info("Stopping Telegram polling ...")
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    # ──────────────────────────────────────────────────────────
    # Push Notifications (called by KuberaBot core)
    # ──────────────────────────────────────────────────────────

    async def notify(self, symbol: str, event: str, detail: str) -> None:
        """Send a trade lifecycle update to the correct chat."""
        chat_id = self._trade_chats.get(symbol)
        if chat_id is None or self._app is None:
            return
        try:
            msg = _fmt_trade_update(symbol, event, detail)
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            logger.warning("[%s] Telegram notify failed: %s", symbol, exc)

    async def send_message(self, chat_id: int, text: str, md: bool = True) -> None:
        """Generic send helper with MarkdownV2 support."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2 if md else None,
            )
        except Exception as exc:
            logger.warning("Telegram send_message failed: %s", exc)

    # ──────────────────────────────────────────────────────────
    # Signal Handler (main pipeline entry)
    # ──────────────────────────────────────────────────────────

    async def _on_signal(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return

        text = update.message.text.strip()
        chat_id = update.effective_chat.id

        await update.message.reply_text(
            "🔍 Signal received\\. Analysing \\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        # Parse signal
        try:
            parsed = _parser.parse(text)
            raw = RawSignal.from_parsed(parsed)
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ *Parse error*: `{str(exc)[:200]}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Register which chat this symbol is trading from
        self._trade_chats[raw.symbol] = chat_id

        # Run full pipeline in background (non-blocking)
        asyncio.create_task(
            self._pipeline_and_report(raw, chat_id),
            name=f"tg_pipeline_{raw.symbol}_{int(time.time())}",
        )

    async def _pipeline_and_report(self, raw: RawSignal, chat_id: int) -> None:
        """Run the full 6-phase pipeline and push updates to Telegram."""
        kubera = self.kubera
        symbol = raw.symbol

        # Load exchange info first (non-blocking)
        await kubera._exec.load_exchange_info(symbol)

        # Phase 1–2: Evaluate
        decision: SignalDecision = await kubera._engine.evaluate(raw)
        await self.send_message(chat_id, _fmt_decision(decision))

        if decision.action == "REJECT":
            self._trade_chats.pop(symbol, None)
            return

        # Phase 3: Risk sizing
        filters = kubera._exec._get_symbol_filters(symbol)
        result = await kubera._risk.compute(
            balance=kubera._balance,
            sl_distance=decision.sl_distance,
            step_size=filters["step_size"],
            reduce_size=decision.reduce_size,
        )

        if not result.allowed:
            await self.send_message(
                chat_id,
                f"🛑 *Trade blocked by RiskEngine*\n`{result.status.value}: {result.reason}`",
            )
            self._trade_chats.pop(symbol, None)
            return

        decision.risk_pct = result.risk_pct
        decision.position_size = result.position_size

        mode_tag = "📄 PAPER" if cfg.PAPER_TRADING else "🔴 LIVE"
        await self.send_message(
            chat_id,
            f"⏳ {mode_tag} \\| Waiting for price to enter zone\\.\\.\\.",
        )

        # Phase 4–5: Execute
        trade = await kubera._exec.execute(decision)
        if trade is None:
            await self.send_message(
                chat_id,
                f"⏰ *\\[{symbol}\\]* Zone timeout — signal expired\\.",
            )
            self._trade_chats.pop(symbol, None)
            return

        paper_tag = "\\[PAPER\\] " if cfg.PAPER_TRADING else ""
        await self.send_message(
            chat_id,
            f"📥 {paper_tag}*\\[{symbol}\\]* ENTERED"
            f"\nDir: `{trade.direction}` Entry: `{trade.entry_price:.4f}`"
            f" SL: `{trade.stop_loss:.4f}`"
            f" Size: `{trade.position_size:.6f}`",
        )

        # Wait for lifecycle and report updates
        await kubera._track_until_closed(
            trade, decision, result.risk_pct,
            notify_fn=self._make_notifier(chat_id, symbol),
        )

        self._trade_chats.pop(symbol, None)

    def _make_notifier(self, chat_id: int, symbol: str):
        """Return a coroutine-compatible callback for trade lifecycle events."""
        async def _notify(event: str, detail: str) -> None:
            await self.send_message(
                chat_id,
                _fmt_trade_update(symbol, event, detail),
            )
        return _notify

    # ──────────────────────────────────────────────────────────
    # Command Handlers
    # ──────────────────────────────────────────────────────────

    async def _cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return
        mode = "📄 PAPER" if cfg.PAPER_TRADING else "🔴 LIVE"
        text = (
            f"🤖 *KUBERA 2\\.0* — Autonomous Signal Engine \\| {mode}\n\n"
            "*Send a signal as a message:*\n"
            "```\n"
            "GOLD SELL @ 4992-4995\n"
            "SL 5000\n"
            "TP1 4986\n"
            "TP2 4974\n"
            "```\n\n"
            "*Commands:*\n"
            "/status \\— open trades\n"
            "/cancel XAUUSDT \\— cancel a trade\n"
            "/stats \\— rolling performance\n"
            "/balance \\— wallet balance\n"
            "/paper \\— toggle paper mode\n"
            "/help \\— this message"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return
        trades = await self.kubera._exec.get_all_open_trades()
        if not trades:
            await update.message.reply_text(
                "📭 No open trades\\.", parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        lines = ["📋 *Open Trades*\n```"]
        for sym, t in trades.items():
            elapsed = int(time.time() - t.open_time)
            lines.append(
                f"{sym:<12} {t.direction:<4} "
                f"entry={t.entry_price:.4f}  "
                f"sl={t.stop_loss:.4f}  "
                f"size={t.position_size:.4f}  "
                f"state={t.state.name}  "
                f"age={elapsed}s"
            )
        lines.append("```")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def _cmd_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /cancel SYMBOL \\(e\\.g\\. /cancel XAUUSDT\\)",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        symbol = context.args[0].upper()
        if not self.kubera._exec.has_open_trade(symbol):
            await update.message.reply_text(
                f"⚠️ No open trade for `{symbol}`\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        await self.kubera._exec.cancel(symbol)
        await update.message.reply_text(
            f"🚫 `{symbol}` trade cancelled\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return
        stats = self.kubera._perf.get_stats()
        await update.message.reply_text(
            _fmt_stats(stats), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def _cmd_balance(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return
        balance = await self.kubera._fetch_balance()
        self.kubera._balance = balance
        mode_tag = "📄 Paper" if cfg.PAPER_TRADING else "🔴 Live"
        await update.message.reply_text(
            f"💰 *Balance \\({mode_tag}\\)*: `{balance:.2f} USDT`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_paper(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not _authorised(update):
            return
        # Toggle paper mode at runtime (restarts exec engine internally)
        cfg.PAPER_TRADING = not cfg.PAPER_TRADING
        self.kubera._exec.paper = cfg.PAPER_TRADING
        mode = "📄 PAPER" if cfg.PAPER_TRADING else "🔴 LIVE"
        await update.message.reply_text(
            f"🔄 Mode switched to *{mode}*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.warning(
            "Paper mode toggled to %s by Telegram user %s",
            cfg.PAPER_TRADING,
            update.effective_user.id,
        )
