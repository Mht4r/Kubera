# 🤖 Binance Futures Autonomous Quant Trading Bot

An **intelligent signal execution engine** for Binance Futures. It never blindly follows signals — it runs every signal through a 6-phase reasoning pipeline before placing (or rejecting) any trade.

---

## Architecture

```
kubera2/
├── bot/
│   ├── __init__.py
│   ├── config.py             ← All settings (env-driven)
│   ├── utils.py              ← Logging, retry, math helpers
│   ├── market_context.py     ← Candles, EMA/ATR, confidence scoring
│   ├── signal_engine.py      ← Phase 1–2: RR analysis + optimization
│   ├── risk_engine.py        ← Phase 3: Adaptive position sizing
│   ├── execution.py          ← Phase 4–5: Entry + exit management
│   ├── performance_tracker.py← Phase 6: Stats, CSV logs, filter control
│   └── main.py               ← Async orchestrator
├── .env.example
├── requirements.txt
├── trades.csv                ← Auto-created
├── state.json                ← Auto-created
└── bot.log                   ← Auto-created
```

---

## 6-Phase Reasoning Pipeline

| Phase | Module | What it does |
|-------|--------|-------------|
| 1 | `signal_engine` | Parse signal, compute RR, optimize if needed, reject if RR < 1:1.5 |
| 2 | `market_context` | Score trend/momentum/volatility/volume — reject or reduce on weak alignment |
| 3 | `risk_engine` | Adaptive risk %, position sizing, drawdown guards |
| 4 | `execution` | Wait for zone entry + candle confirmation |
| 5 | `execution` | TP1 partial close, breakeven SL, ATR trail, stall reduction |
| 6 | `performance_tracker` | Rolling stats; tighten filters if expectancy goes negative |

---

## Quick Setup

### 1. Clone / Copy Project
```bash
# On your VPS or local machine
cd ~/aladin
```

### 2. Create Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate          # Linux/Mac
# .\venv\Scripts\activate         # Windows
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
```bash
cp .env.example .env
nano .env  # Add your Binance API keys
```

Key settings in `.env`:
```
BINANCE_API_KEY=your_key
BINANCE_SECRET_KEY=your_secret
PAPER_TRADING=true    ←  Start here!
```

---

## Running the Bot

### Paper Trading (Recommended First Step)
```bash
# Ensure PAPER_TRADING=true in .env
python -m bot.main
```

### Self-Test (No API Keys Required)
```bash
python -m bot.main --selftest
```
Runs 3 built-in test signals and prints decisions/scores.

### Live Trading
```bash
# Set PAPER_TRADING=false and add real API keys in .env
python -m bot.main
```

---

## Sending a Signal

Paste **one JSON object per line** on stdin:

```json
{"symbol":"XAUUSDT","direction":"SELL","entry_low":5070,"entry_high":5073,"stop_loss":5090,"take_profits":[5065,5060,5050]}
```

### Signal Format
```json
{
  "symbol": "XAUUSDT",         // Binance Futures symbol
  "direction": "SELL",         // "BUY" or "SELL"
  "entry_low": 5070,           // Entry zone low
  "entry_high": 5073,          // Entry zone high
  "stop_loss": 5090,           // Hard stop loss price
  "take_profits": [5065, 5060, 5050],   // Array of TPs (TP1 = 50%, rest = runner)
  "news_flag": false           // Optional: true = reject due to news risk
}
```

### Example Decision Output
```
────────────────────────────────────────────────────────────
  TRADE DECISION : EXECUTE
  Symbol         : XAUUSDT
  Direction      : SELL
  Entry          : 5071.5000
  Stop Loss      : 5090.0000
  Take Profits   : ['5065.0000', '5060.0000', '5050.0000']
  SL Distance    : 18.5000
  Blended RR     : 1:1.84
  Confidence     : 72.0/100
  Risk %         : 0.50%
  Position Size  : 0.027
  Capital Status : NORMAL
  Justification  : RR=1.84 | Confidence=72 | HTF=BEAR | ATR=12.3 | VolRatio=1.42
────────────────────────────────────────────────────────────
```

---

## Key Rules (Always Enforced)

| Rule | Detail |
|------|--------|
| Min RR | 1:1.5 — optimized before rejection |
| Max risk | 1% per trade (hard cap) |
| Max open | 1 trade per symbol |
| Drawdown pause | >5% → stop taking new signals |
| Drawdown halt | >8% → system halt |
| Never widen SL | Enforced at code level |
| Consecutive losses | 3 losses → risk halved to 0.25% |
| Low confidence | <40/100 → auto-reject |
| Weak alignment | 40–60/100 → size halved |

---

## VPS Deployment (Ubuntu 22.04)

```bash
# 1. Install Python
sudo apt update && sudo apt install python3.11 python3.11-venv python3.11-dev -y

# 2. Set up project
git clone <your-repo> ~/aladin
cd ~/aladin
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Speed boost (Linux only)
pip install uvloop

# 4. Configure
cp .env.example .env
nano .env   # Add API keys, set PAPER_TRADING=false when ready

# 5. Run as a background service with systemd
sudo nano /etc/systemd/system/tradingbot.service
```

**systemd service file:**
```ini
[Unit]
Description=Binance Futures Trading Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/aladin
ExecStart=/home/ubuntu/aladin/venv/bin/python -m bot.main
Restart=always
RestartSec=10
User=ubuntu
EnvironmentFile=/home/ubuntu/aladin/.env

[Install]
WantedBy=multi-user.target
```

```bash
# 6. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable tradingbot
sudo systemctl start tradingbot

# 7. Monitor logs
sudo journalctl -u tradingbot -f
# or
tail -f bot.log
```

---

## Output Files

| File | Contents |
|------|----------|
| `bot.log` | Structured timestamped log of every decision |
| `trades.csv` | Complete trade history with RR, PnL, duration |
| `state.json` | Persistent risk state (drawdown, streak) |

---

## Binance API Key Setup

1. Go to [Binance Futures](https://www.binance.com/en/futures)
2. Account → API Management → Create API
3. Enable **Futures Trading** permission
4. **Do NOT enable Withdrawals**
5. Whitelist your VPS IP (highly recommended)
6. Add keys to `.env`

> ⚠️ Never commit `.env` to version control.
