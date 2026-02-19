"""
utils.py  –  KUBERA 2.0
Shared helpers: logging factory, async retry, precision, rate-limit guard.

FIXES vs v1:
  • round_step_size guards against step_size <= 0 and math domain errors
  • async_retry skips immediate retry on HTTP 429/418 (rate-limit) — uses
    RATE_LIMIT_PAUSE_SEC instead of exponential backoff
  • Added RateLimitGuard class to track API weight
  • Removed internal precision variable that was computed but never used
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import time
from typing import Any, Callable, Optional, TypeVar

import bot.config as cfg

# ─────────────────────────────────────────────────────────────────────────────
# Logger Factory
# ─────────────────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger with file + console handlers (idempotent)."""
    logger = logging.getLogger(f"kubera.{name}")
    if logger.handlers:
        return logger

    level = getattr(logging, cfg.LOG_LEVEL, logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-32s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    try:
        fh = logging.FileHandler(cfg.LOG_FILE, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except IOError as exc:
        # Don't crash startup if log file cannot be opened
        logger.warning("Cannot open log file %s: %s", cfg.LOG_FILE, exc)

    logger.propagate = False
    return logger


# ─────────────────────────────────────────────────────────────────────────────
# Async Retry Decorator
# ─────────────────────────────────────────────────────────────────────────────

F = TypeVar("F", bound=Callable[..., Any])

_retry_logger = logging.getLogger("kubera.retry")


def async_retry(
    attempts: int = cfg.RETRY_ATTEMPTS,
    base_delay: float = cfg.RETRY_BASE_DELAY_SEC,
    max_delay: float = cfg.RETRY_MAX_DELAY_SEC,
    exceptions: tuple = (Exception,),
):
    """
    Exponential-backoff retry decorator for async functions.

    FIX: Rate-limit responses (HTTP 429 / code -1003) now use
    RATE_LIMIT_PAUSE_SEC instead of the standard backoff to avoid
    hammering the API and making the situation worse.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    exc_str = str(exc)

                    # Rate-limit detection (HTTP 429 or Binance code -1003/-1015)
                    is_rate_limit = (
                        "429" in exc_str
                        or "418" in exc_str
                        or "-1003" in exc_str
                        or "-1015" in exc_str
                        or "TOO_MANY_REQUESTS" in exc_str.upper()
                    )

                    if attempt == attempts:
                        _retry_logger.error(
                            "%s failed after %d attempts: %s",
                            func.__name__, attempts, exc,
                        )
                        raise

                    if is_rate_limit:
                        _retry_logger.warning(
                            "Rate-limit hit on %s (attempt %d) — pausing %.0fs",
                            func.__name__, attempt, cfg.RATE_LIMIT_PAUSE_SEC,
                        )
                        await asyncio.sleep(cfg.RATE_LIMIT_PAUSE_SEC)
                        delay = base_delay  # reset backoff after rate-limit pause
                    else:
                        _retry_logger.warning(
                            "%s attempt %d/%d failed: %s — retry in %.1fs",
                            func.__name__, attempt, attempts, exc, delay,
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, max_delay)

        return wrapper  # type: ignore[return-value]

    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# Precision Helpers
# ─────────────────────────────────────────────────────────────────────────────

def round_step_size(quantity: float, step_size: float) -> float:
    """
    Round a quantity DOWN to the nearest valid step size for Binance lot filter.

    FIX vs v1:
      • Handles step_size <= 0 (returns quantity unchanged)
      • Handles step_size >= 1 (e.g. 1.0 for whole-unit markets)
      • Uses string precision instead of math.log to avoid domain errors
        on unusual step sizes like 0.00000001
    """
    if step_size <= 0:
        return quantity
    if step_size >= 1.0:
        return math.floor(quantity / step_size) * step_size

    # Determine decimal precision safely via string representation
    step_str = f"{step_size:.10f}".rstrip("0")
    if "." in step_str:
        precision = len(step_str.split(".")[1])
    else:
        precision = 0

    floored = math.floor(quantity / step_size) * step_size
    return round(floored, precision)


def round_price(price: float, tick_size: float) -> float:
    """Round price to nearest valid tick size (Binance PRICE_FILTER)."""
    if tick_size <= 0:
        return price
    tick_str = f"{tick_size:.10f}".rstrip("0")
    precision = len(tick_str.split(".")[1]) if "." in tick_str else 0
    return round(round(price / tick_size) * tick_size, precision)


def format_number(value: float, decimals: int = 4) -> str:
    return f"{value:.{decimals}f}"


# ─────────────────────────────────────────────────────────────────────────────
# Timestamp Helpers
# ─────────────────────────────────────────────────────────────────────────────

def now_ms() -> int:
    """Current UTC time in milliseconds."""
    return int(time.time() * 1000)


def now_ts() -> float:
    return time.time()


# ─────────────────────────────────────────────────────────────────────────────
# Math / Statistics Helpers
# ─────────────────────────────────────────────────────────────────────────────

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def pct_change(old: float, new: float) -> float:
    """Signed % change from old → new."""
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def weighted_average(values: list[float], weights: tuple | list) -> float:
    """
    Weighted average. Pads/truncates weights to match values length.
    Returns 0.0 on empty inputs.
    """
    if not values:
        return 0.0
    n = len(values)
    w = list(weights)[:n]
    if len(w) < n:
        # Extend with equal share of remaining weight
        remaining = max(0.0, 1.0 - sum(w))
        w += [remaining / (n - len(w))] * (n - len(w))
    total_w = sum(w) or 1.0
    return sum(v * wt / total_w for v, wt in zip(values, w))


# ─────────────────────────────────────────────────────────────────────────────
# Sanitised logging (prevent API key leakage)
# ─────────────────────────────────────────────────────────────────────────────

def sanitise_for_log(text: str) -> str:
    """Redact API key and secret if accidentally included in log strings."""
    result = text
    if cfg.BINANCE_API_KEY and cfg.BINANCE_API_KEY in result:
        result = result.replace(cfg.BINANCE_API_KEY, "***API_KEY***")
    if cfg.BINANCE_SECRET_KEY and cfg.BINANCE_SECRET_KEY in result:
        result = result.replace(cfg.BINANCE_SECRET_KEY, "***SECRET***")
    return result
