"""
signal_parser.py  –  KUBERA 2.0
Parses both raw text signals (Telegram/channel format) and structured
JSON dicts into a validated RawSignal object.

Supported text formats
──────────────────────
  📉 GOLD SELL 5070/5073
  TP 5065
  TP 5060
  TP 5050 Open
  SL 5090

  BTC LONG 95000-95200
  SL: 94000
  TP1: 96000
  TP2: 97000

  XAUUSDT SELL @ 5071.5
  SL 5090
  TP 5065/5060/5050

Symbol aliases (channel shorthand → Binance symbol)
────────────────────────────────────────────────────
  GOLD  → XAUUSDT
  BTC   → BTCUSDT
  ETH   → ETHUSDT
  SOL   → SOLUSDT
  BNB   → BNBUSDT
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bot.utils import get_logger

logger = get_logger("signal_parser")

# ─────────────────────────────────────────────────────────────────────────────
# Symbol alias map
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL_ALIASES: dict[str, str] = {
    "GOLD": "XAUUSDT",
    "XAU": "XAUUSDT",
    "XAUUSD": "XAUUSDT",   # common shorthand without the trailing T
    "BTC": "BTCUSDT",
    "BITCOIN": "BTCUSDT",
    "ETH": "ETHUSDT",
    "ETHEREUM": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "DOGE": "DOGEUSDT",
    "LINK": "LINKUSDT",
    "ADA": "ADAUSDT",
    "DOT": "DOTUSDT",
    "AVAX": "AVAXUSDT",
    "MATIC": "MATICUSDT",
}

# Direction keywords → standard BUY/SELL
DIRECTION_MAP: dict[str, str] = {
    "SELL": "SELL", "SHORT": "SELL", "BEAR": "SELL",
    "BUY": "BUY", "LONG": "BUY", "BULL": "BUY",
    "📉": "SELL", "📈": "BUY",
    "🔴": "SELL", "🟢": "BUY",
    "⬇": "SELL", "⬆": "BUY",
}


# ─────────────────────────────────────────────────────────────────────────────
# Parsed output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedSignal:
    """Intermediate parsed result (before geometry validation)."""
    symbol: str
    direction: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profits: list[float]
    news_flag: bool = False
    raw_text: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_loss": self.stop_loss,
            "take_profits": self.take_profits,
            "news_flag": self.news_flag,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Parser class
# ─────────────────────────────────────────────────────────────────────────────

class SignalParser:
    """
    Parses free-form text signals into structured ParsedSignal objects.
    Handles the full variety of Telegram channel signal formats.
    """

    # Numbers: 12345, 12345.67, 12,345 — commas stripped on input
    _NUM = r"[\d]+(?:\.\d+)?"
    # Separator chars for ranges: hyphen, en-dash (–), em-dash (—), slash, plus
    _SEP = r"[/\-\+\u2013\u2014]+"


    def parse_text(self, text: str) -> ParsedSignal:
        """
        Parse a free-form text signal.
        Raises ValueError if the signal cannot be parsed.
        """
        # Normalise: uppercase, strip commas in numbers, collapse whitespace
        text_clean = (
            text.upper()
            .replace(",", "")
            .replace("\u2013", "-")   # en dash → hyphen
            .replace("\u2014", "-")   # em dash → hyphen
        )
        lines = [ln.strip() for ln in text_clean.splitlines() if ln.strip()]

        symbol = self._extract_symbol(lines)
        direction = self._extract_direction(lines)
        entry_low, entry_high = self._extract_entry(lines)
        stop_loss = self._extract_sl(lines)
        take_profits = self._extract_tps(lines)

        if not take_profits:
            raise ValueError("No take-profit levels found in signal text")
        if stop_loss is None:
            raise ValueError("No stop-loss found in signal text")

        return ParsedSignal(
            symbol=symbol,
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=sorted(take_profits, reverse=(direction == "SELL")),
            raw_text=text,
        )

    def parse_json(self, data: dict) -> ParsedSignal:
        """
        Parse a structured JSON/dict signal.
        Accepts both the original format and the text format dict.
        """
        try:
            symbol = self._normalise_symbol(str(data["symbol"]).upper())
            direction = DIRECTION_MAP.get(str(data["direction"]).upper(), "").upper()
            if direction not in ("BUY", "SELL"):
                raise ValueError(f"Unknown direction: {data['direction']}")

            entry_raw = data.get("entry", data.get("entry_low", data.get("entry_high")))
            entry_low = float(data.get("entry_low", entry_raw))
            entry_high = float(data.get("entry_high", entry_raw))

            # Ensure low ≤ high
            if entry_low > entry_high:
                entry_low, entry_high = entry_high, entry_low

            sl = float(data["stop_loss"])
            tps = [float(t) for t in data["take_profits"]]
            if not tps:
                raise ValueError("take_profits list is empty")

            return ParsedSignal(
                symbol=symbol,
                direction=direction,
                entry_low=entry_low,
                entry_high=entry_high,
                stop_loss=sl,
                take_profits=sorted(tps, reverse=(direction == "SELL")),
                news_flag=bool(data.get("news_flag", False)),
            )
        except KeyError as e:
            raise ValueError(f"Missing required field: {e}") from e

    def parse(self, source: str | dict) -> ParsedSignal:
        """Auto-detect input type and parse."""
        if isinstance(source, dict):
            result = self.parse_json(source)
        else:
            result = self.parse_text(str(source))
        logger.info(
            "Parsed signal: %s %s entry=[%.4f–%.4f] SL=%.4f TPs=%s",
            result.symbol, result.direction, result.entry_low, result.entry_high,
            result.stop_loss, result.take_profits,
        )
        return result

    # ──────────────────────────────────────────────────────────
    # Extraction helpers
    # ──────────────────────────────────────────────────────────

    def _extract_symbol(self, lines: list[str]) -> str:
        """Find symbol token in any line."""
        # Try explicit USDT suffix first
        for line in lines:
            m = re.search(r"\b([A-Z]{2,10}USDT)\b", line)
            if m:
                return m.group(1)

        # Try known aliases
        for line in lines:
            tokens = re.split(r"[\s/\-@|\+]+", line)
            for token in tokens:
                token = re.sub(r"[^A-Z]", "", token)
                if token in SYMBOL_ALIASES:
                    return SYMBOL_ALIASES[token]

        raise ValueError("Could not extract symbol from signal")

    def _extract_direction(self, lines: list[str]) -> str:
        """Find BUY/SELL direction in any line."""
        all_text = " ".join(lines)
        # Emoji first (higher specificity)
        for token, direction in DIRECTION_MAP.items():
            if token in all_text:
                return direction
        # Word tokens
        tokens = re.split(r"\s+", all_text)
        for token in tokens:
            clean = re.sub(r"[^A-Z]", "", token)
            if clean in DIRECTION_MAP:
                return DIRECTION_MAP[clean]

        raise ValueError("Could not extract direction (BUY/SELL) from signal")

    def _extract_entry(self, lines: list[str]) -> tuple[float, float]:
        """
        Extract entry zone as (low, high). Single value → low == high.

        Handles all of these real-world patterns:
          GOLD SELL 5070/5073           → range with /
          GOLD SELL 5070-5073           → range with -
          GOLD SELL @ 5071              → @ prefix (single)
          Entry: 5070 - 5073            → labeled range
          Entry: 5073                   → labeled single
          Entry 5073                    → labeled single (no colon)
          ENTRY ZONE: 5070/5073         → labeled range
          NOW: 5071  /  PRICE: 5071     → now/price prefix
          BUY 5070  or  SELL 5073       → direction + number
          GOLD SELL\\n5070-5073          → range on next line
          🔴 SELL\\n5070                 → lone number on its own line (last resort)
        """
        num = self._NUM
        all_text = " ".join(lines)

        # ── 1. Labeled range: "Entry: 5070-5073" / "Entry 5070/5073" / "Entry 5188+5180" ─────
        labeled_range = re.compile(
            rf"(?:ENTRY(?:\s+ZONE)?|ENTRAR|ENTER)[:\s]+({num})\s*[/\-\+]\s*({num})", re.I
        )
        m = labeled_range.search(all_text)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            return (min(lo, hi), max(lo, hi))

        # ── 2. Labeled single: "Entry: 5071" / "Entry Now: 5071" ─────────
        labeled_single = re.compile(
            rf"(?:ENTRY(?:\s+(?:NOW|ZONE|POINT|PRICE))?|ENTRAR|ENTER)[:\s]+({num})\b", re.I
        )
        m = labeled_single.search(all_text)
        if m:
            v = float(m.group(1))
            return v, v

        # ── 3. @ prefix: "@ 5071" ─────────────────────────────────────────
        at_pat = re.compile(rf"@\s*({num})\b")
        m = at_pat.search(all_text)
        if m:
            v = float(m.group(1))
            return v, v

        # ── 4. NOW / PRICE prefix: "NOW: 5071" / "PRICE: 5071" ───────────
        now_pat = re.compile(rf"(?:NOW|CURRENT\s+PRICE|PRICE)[:\s]+({num})\b", re.I)
        m = now_pat.search(all_text)
        if m:
            v = float(m.group(1))
            return v, v

        # ── 5. Direction + range on same token: "SELL 5070/5073" ──────────
        dir_range_pat = re.compile(
            rf"(?:BUY|SELL|LONG|SHORT|BULLISH|BEARISH)\s+({num})\s*[/\-\+]\s*({num})\b", re.I
        )
        m = dir_range_pat.search(all_text)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            return (min(lo, hi), max(lo, hi))

        # ── 6. Direction + single: "SELL 5073" ────────────────────────────
        dir_single_pat = re.compile(
            rf"(?:BUY|SELL|LONG|SHORT|BULLISH|BEARISH)\s+({num})\b", re.I
        )
        m = dir_single_pat.search(all_text)
        if m:
            v = float(m.group(1))
            return v, v

        # ── 7. Bare range anywhere in text (last resort): "5070-5073" / "5188+5180" ─────
        #    Guard: both numbers must be > 100 (avoid matching SL/TP by mistake)
        bare_range = re.compile(rf"({num})\s*[/\-\+]\s*({num})")
        for m in bare_range.finditer(all_text):
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo > 100 and hi > 100 and lo != hi:
                return (min(lo, hi), max(lo, hi))

        # ── 8. Lone large number on its own line (absolute last resort) ───
        #    Pick the first line that is purely a number and looks like a price
        for line in lines:
            m = re.fullmatch(rf"\s*({num})\s*", line)
            if m:
                v = float(m.group(1))
                if v > 100:   # heuristic: prices are at least 100
                    return v, v

        raise ValueError(
            "Could not extract entry price/zone from signal\\. "
            "Please use a format like:\\n"
            "`GOLD SELL 3290-3295\\nSL 3310\\nTP1 3275`"
        )

    def _extract_sl(self, lines: list[str]) -> Optional[float]:
        """Extract stop loss value."""
        num = self._NUM
        pat = re.compile(rf"(?:SL|STOP[\s_\-]*LOSS|STOP)[:\s@]*({num})", re.I)
        for line in lines:
            m = pat.search(line)
            if m:
                return float(m.group(1))
        return None

    def _extract_tps(self, lines: list[str]) -> list[float]:
        """Extract all take-profit levels."""
        num = self._NUM
        tps: list[float] = []

        # Explicit TP lines: "TP 5065", "TP1: 5060", "T/P 5050", "TP5200" (no space)
        tp_line = re.compile(rf"(?:TP|T/P|TAKE[\s_\-]*PROFIT)(?:[1-9](?!\d))?[:\s]*({num})", re.I)
        for line in lines:
            m = tp_line.search(line)
            if m:
                tps.append(float(m.group(1)))

        # Slash-separated TPs: "TP 5065/5060/5050"
        if not tps:
            slash_pat = re.compile(rf"(?:TP|T/P)[:\s]*({num}(?:\s*/\s*{num})+)", re.I)
            m = slash_pat.search(" ".join(lines))
            if m:
                tps = [float(x.strip()) for x in m.group(1).split("/")]

        return tps

    @staticmethod
    def _normalise_symbol(raw: str) -> str:
        sym = re.sub(r"[^A-Z0-9]", "", raw.upper())
        if sym in SYMBOL_ALIASES:
            return SYMBOL_ALIASES[sym]
        if not sym.endswith("USDT") and not sym.endswith("BUSD"):
            sym = sym + "USDT"
        return sym
