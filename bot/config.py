"""
config.py  –  KUBERA 2.0
Central configuration & environment loader.
All magic numbers live here — never hard-code them elsewhere.

FIXES vs v1:
  • Added LEVERAGE setting (critical — position sizing assumes known leverage)
  • Added API_RATE_LIMIT_BUFFER_PCT to prevent weight exhaustion
  • TP_WEIGHTS changed from list → tuple (immutable)
  • Added EXCHANGE_INFO_CACHE_TTL to avoid re-fetching 500-symbol payload
  • Added STARTUP_RECONCILE flag
"""

import os
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "KUBERA 2.0"
BOT_VERSION = "2.0.0"

# ─────────────────────────────────────────────────────────────────────────────
# API Credentials  (NEVER log these values)
# ─────────────────────────────────────────────────────────────────────────────
BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY: str = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_TESTNET: bool = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
# Telegram Bot  (NEVER log these values)
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

def _parse_allowed_users() -> frozenset:
    raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)

TELEGRAM_ALLOWED_USERS: frozenset = _parse_allowed_users()

# ─────────────────────────────────────────────────────────────────────────────
# Mode
# ─────────────────────────────────────────────────────────────────────────────
PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
STARTUP_RECONCILE: bool = os.getenv("STARTUP_RECONCILE", "true").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
# Leverage
# ─────────────────────────────────────────────────────────────────────────────
# CRITICAL: position sizing uses SL-distance in price units / contract size.
# Bot sets this leverage on startup. Risk % formula is based on notional exposure.
LEVERAGE: int = int(os.getenv("LEVERAGE", "10"))

# ─────────────────────────────────────────────────────────────────────────────
# Risk Parameters
# ─────────────────────────────────────────────────────────────────────────────
BASE_RISK_PCT: float = float(os.getenv("BASE_RISK_PCT", "0.5"))
MAX_RISK_PCT: float = float(os.getenv("MAX_RISK_PCT", "1.0"))
HIGH_WIN_RISK_PCT: float = float(os.getenv("HIGH_WIN_RISK_PCT", "0.75"))
LOW_STREAK_RISK_PCT: float = float(os.getenv("LOW_STREAK_RISK_PCT", "0.25"))
HIGH_WIN_THRESHOLD: float = float(os.getenv("HIGH_WIN_THRESHOLD", "60.0"))
CONSECUTIVE_LOSS_TRIGGER: int = int(os.getenv("CONSECUTIVE_LOSS_TRIGGER", "3"))

# Drawdown guards
DRAWDOWN_PAUSE_PCT: float = float(os.getenv("DRAWDOWN_PAUSE_PCT", "5.0"))
DRAWDOWN_HALT_PCT: float = float(os.getenv("DRAWDOWN_HALT_PCT", "8.0"))

# ─────────────────────────────────────────────────────────────────────────────
# Signal / RR Filters
# ─────────────────────────────────────────────────────────────────────────────
MIN_RR: float = float(os.getenv("MIN_RR", "1.5"))
MIN_RR_STRICT: float = float(os.getenv("MIN_RR_STRICT", "2.0"))
MAX_SPREAD_PCT_OF_SL: float = float(os.getenv("MAX_SPREAD_PCT_OF_SL", "10.0"))
MAX_PRICE_MOVED_TO_TP_PCT: float = float(os.getenv("MAX_PRICE_MOVED_TO_TP_PCT", "50.0"))

# ─────────────────────────────────────────────────────────────────────────────
# Market Context / Indicators
# ─────────────────────────────────────────────────────────────────────────────
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))
EMA_FAST: int = int(os.getenv("EMA_FAST", "20"))
EMA_MID: int = int(os.getenv("EMA_MID", "50"))
EMA_SLOW: int = int(os.getenv("EMA_SLOW", "200"))
CANDLE_LIMIT: int = int(os.getenv("CANDLE_LIMIT", "250"))
CONFIDENCE_REJECT_THRESHOLD: int = int(os.getenv("CONFIDENCE_REJECT_THRESHOLD", "40"))
CONFIDENCE_REDUCE_THRESHOLD: int = int(os.getenv("CONFIDENCE_REDUCE_THRESHOLD", "60"))
TIMEFRAMES: list[str] = ["1m", "5m", "1h"]

# Exchange info cache TTL — avoids fetching 500-symbol payload on every signal
EXCHANGE_INFO_CACHE_TTL: float = float(os.getenv("EXCHANGE_INFO_CACHE_TTL", "3600.0"))

# ─────────────────────────────────────────────────────────────────────────────
# Entry / Exit
# ─────────────────────────────────────────────────────────────────────────────
ENTRY_POLL_INTERVAL_SEC: float = float(os.getenv("ENTRY_POLL_INTERVAL_SEC", "1.0"))
ENTRY_ZONE_TIMEOUT_MIN: float = float(os.getenv("ENTRY_ZONE_TIMEOUT_MIN", "60.0"))
POSITION_MONITOR_INTERVAL_SEC: float = float(os.getenv("POSITION_MONITOR_INTERVAL_SEC", "2.0"))
ATR_TRAIL_MULTIPLIER: float = float(os.getenv("ATR_TRAIL_MULTIPLIER", "1.0"))
TP1_SIZE_PCT: float = float(os.getenv("TP1_SIZE_PCT", "50.0"))
STALL_CANDLES: int = int(os.getenv("STALL_CANDLES", "30"))

# ─────────────────────────────────────────────────────────────────────────────
# Performance / Self-Awareness
# ─────────────────────────────────────────────────────────────────────────────
PERF_ROLLING_WINDOW: int = int(os.getenv("PERF_ROLLING_WINDOW", "20"))
TRADES_CSV_PATH: str = os.getenv("TRADES_CSV_PATH", "trades.csv")
STATE_JSON_PATH: str = os.getenv("STATE_JSON_PATH", "state.json")

# ─────────────────────────────────────────────────────────────────────────────
# Logging  (NEVER log API keys or secret values)
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE: str = os.getenv("LOG_FILE", "kubera.log")

# ─────────────────────────────────────────────────────────────────────────────
# API Retry & Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────
RETRY_ATTEMPTS: int = int(os.getenv("RETRY_ATTEMPTS", "5"))
RETRY_BASE_DELAY_SEC: float = float(os.getenv("RETRY_BASE_DELAY_SEC", "1.0"))
RETRY_MAX_DELAY_SEC: float = float(os.getenv("RETRY_MAX_DELAY_SEC", "30.0"))
# Don't retry on 429/418 with standard backoff — use longer pause
RATE_LIMIT_PAUSE_SEC: float = float(os.getenv("RATE_LIMIT_PAUSE_SEC", "60.0"))

# ─────────────────────────────────────────────────────────────────────────────
# TP Weight Distribution — immutable tuple (must sum to 1.0)
# Used for blended RR calculation. Extra TPs share the last weight.
# ─────────────────────────────────────────────────────────────────────────────
TP_WEIGHTS: tuple[float, ...] = (0.50, 0.30, 0.20)

# ─────────────────────────────────────────────────────────────────────────────
# Smart Money Concepts (SMC) Engine
# ─────────────────────────────────────────────────────────────────────────────
# Set SMC_ENABLED=false to run the older 5-phase pipeline without SMC
SMC_ENABLED:           bool  = os.getenv("SMC_ENABLED", "true").lower() == "true"

# Order Block lookback (candles on 5m; H1 uses OB_LOOKBACK // 5)
SMC_OB_LOOKBACK:       int   = int(os.getenv("SMC_OB_LOOKBACK", "50"))

# Fair Value Gap lookback (candles on 5m)
SMC_FVG_LOOKBACK:      int   = int(os.getenv("SMC_FVG_LOOKBACK", "30"))

# S/R detection lookback (candles on 1h) and minimum touches to qualify
SMC_SR_LOOKBACK:       int   = int(os.getenv("SMC_SR_LOOKBACK", "100"))
SMC_SR_TOUCH_MIN:      int   = int(os.getenv("SMC_SR_TOUCH_MIN", "2"))

# Zone tolerance expressed as a multiple of ATR
# (e.g. 0.3 means price within 0.3×ATR of zone boundary qualifies)
SMC_ZONE_TOLERANCE_ATR: float = float(os.getenv("SMC_ZONE_TOLERANCE_ATR", "0.3"))

# Minimum SMC score (0–30) before SMC adds a partial reject recommendation
# (does NOT hard-reject; only adds a note and reduces soft-confidence)
SMC_WEAK_SCORE_THRESHOLD: float = float(os.getenv("SMC_WEAK_SCORE_THRESHOLD", "5.0"))

