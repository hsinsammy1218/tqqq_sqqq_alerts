# tqqq_sqqq_alerts

QQQ-driven swing-trading alert system that emits alerts for TQQQ/SQQQ/CASH.

This is alert-only software. It does not place orders and has no broker execution code.

## Features

- Uses QQQ as analysis source across daily + 4h context
- Indicators: EMA(20/50), RSI(14), MACD, ATR(14), weekly VWAP, anchored VWAP, volume vs 20-period average
- Scores bullish and bearish setups (0-8 each)
- Emits alerts: BUY, SELL, CASH
- Risk logic: stop loss, take profit, stretch target, max hold
- Outputs: console, CSV journal, Discord webhook
- Dry-run mode for testing without Discord sends
- KlickAnalytics CLI market data backend

## File layout

- `config.py`
- `data.py`
- `indicators.py`
- `strategy.py`
- `alerts.py`
- `journal.py`
- `main.py`
- `.env.example`
- `requirements.txt`
- `events.example.json`

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill values:

- `KLICKANALYTICS_CLI_API_KEY` (required)
- `KLICKANALYTICS_CLI_COMMAND=ka` (override only if your executable name/path differs)
- `DISCORD_WEBHOOK_URL` only required when `DRY_RUN=false`

Optional: copy `events.example.json` to `events.json` and maintain `blocked_dates` for CPI/FOMC/major earnings blackout.

## Run

Dry-run:

```bash
python main.py --dry-run
```

Live Discord alerts:

```bash
python main.py
```

## Decision rules

- BUY TQQQ when bullish score >= 5 and bearish < 5
- BUY SQQQ when bearish score >= 5 and bullish < 5
- CASH when mixed/weak/conflicting
- SELL active side when:
  - active score < 3, or
  - opposite score >= 5, or
  - stop loss / take profit trigger, or
  - max hold > 10 trading days

Mutual exclusivity is enforced: never hold TQQQ and SQQQ simultaneously.

## Sample Discord alert

```text
Alert: BUY
Symbol: TQQQ
QQQ trend: daily close > EMA20; daily EMA20 > EMA50; 4h EMA20 > EMA50
Bullish score: 6/8
Bearish score: 2/8
Confidence: 50%
Entry zone: 432.10 - 439.70
Stop loss: 400.65
Take profit: 502.13
Stretch target: 545.79
Max hold date: 2026-05-15
Timestamp: 2026-05-01T14:20:00Z
Notes: Bullish QQQ setup.
```

## Sample CSV journal format

```csv
timestamp_utc,alert_type,execution_symbol,qqq_trend_reason,bull_score,bear_score,confidence,entry_zone_low,entry_zone_high,stop_price,take_profit_price,take_profit_stretch_price,max_hold_date,notes
2026-05-01T14:20:00Z,BUY,TQQQ,daily close > EMA20; daily EMA20 > EMA50; 4h EMA20 > EMA50,6,2,50,432.1,439.7,400.65,502.13,545.79,2026-05-15,Bullish QQQ setup.
```

## Notes

- 4h candles are synthesized from 1h KlickAnalytics intraday bars.
- Data is pulled via KlickAnalytics CLI commands (`ka prices` and `ka intraday`).
- Real-time quality depends on your KlickAnalytics plan and your run cadence.
- Educational use only. Not financial advice.
