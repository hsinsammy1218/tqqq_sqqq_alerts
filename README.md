# tqqq_sqqq_alerts

QQQ-driven swing-trading alert system that emits alerts for TQQQ/SQQQ/CASH.

This is alert-only software. It does not place orders and has no broker execution code.

## Features

- Uses QQQ as analysis source across daily + 4h context
- Indicators: EMA(20/50), RSI(14), MACD, ATR(14), weekly VWAP, anchored VWAP, volume vs 20-period average
- Scores bullish and bearish setups (0-8 each)
- Emits alerts: BUY, SELL, FLIP, CASH
- Risk logic: stop loss, take profit, stretch target, max hold
- Outputs: console, CSV journal, Discord webhook
- Dry-run mode for testing without Discord sends
- Discord **rich embeds** match the console breakdown: bot memory (flat vs symbol), bar timestamps + blackout, full QQQ daily/4h indicator lines, every bull/bear checklist item, rule thresholds, hold exit flags when applicable, then notes
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

Discord webhook quick setup:

1. In Discord, open **Server Settings -> Integrations -> Webhooks**.
2. Create webhook, choose channel, copy webhook URL.
3. Set `DISCORD_WEBHOOK_URL=<your webhook>` in `.env`.
4. Set `DRY_RUN=false` when you want live posts.

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

After the action line, the bot prints a **technical breakdown**: last bar timestamps, full QQQ daily/4h indicator snapshot, every bull/bear score check that fired, threshold rules, position memory, and (when relevant) hold/exit flags vs stop/TP/max-hold. For a short console log only, use `python main.py --no-technical`.

Tell the bot you have **no brokerage position** (sync bot memory to flat):

```bash
python main.py --flat
```

Use `--flat` once after you’ve closed everything on Robinhood (or on first setup). The next normal run still updates `position_state.json` if a BUY signal appears.

Tell the bot you **already hold** TQQQ or SQQQ (e.g. you bought on Robinhood before the bot tracked it):

```bash
python main.py --set-position TQQQ
python main.py --set-position TQQQ --entry-price 72.50
python main.py --set-position SQQQ --entry-price 14.20 --entry-time 2026-05-01T14:30:00Z
```

Only **which symbol** you hold is required. **`--entry-price`** (your ETF average cost) and **`--entry-time`** are optional: if you skip price, stop/target math uses **QQQ’s daily close** as a stand-in; if you skip time, **max hold** starts from the **first bot run** that finishes after this sync.

You can also edit [`position_state.json`](position_state.json) by hand:

```json
{
  "active_symbol": "TQQQ",
  "entry_price": 72.5,
  "entry_timestamp": "2026-05-01T14:30:00Z"
}
```

Use `null` for `entry_price` / `entry_timestamp` when you only want the bot to know the side. Omit `active_symbol` or run `--flat` when flat.

## Decision rules

- BUY TQQQ when bullish score >= 5 and bearish < 5
- BUY SQQQ when bearish score >= 5 and bullish < 5
- FLIP active side when reversal threshold triggers:
  - holding TQQQ and bearish >= threshold -> SELL TQQQ + BUY SQQQ
  - holding SQQQ and bullish >= threshold -> SELL SQQQ + BUY TQQQ
- CASH when mixed/weak/conflicting
- SELL active side when:
  - active score < 3, or
  - opposite score >= 5, or
  - stop loss / take profit trigger, or
  - max hold > 10 trading days

Mutual exclusivity is enforced: never hold TQQQ and SQQQ simultaneously.

## Sample Discord alert

```text
ACTION: BUY TQQQ
(You trade manually - alerts only, no broker execution.)

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
