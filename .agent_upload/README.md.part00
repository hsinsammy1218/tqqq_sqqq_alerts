# tqqq_sqqq_alerts

QQQ-driven swing-trading alert system that emits alerts for TQQQ/SQQQ/CASH.

This is **alert-only** software: it produces **signals for manual review**. You decide whether to act (for example in Robinhood); **nothing here places trades**.

### Signals you may see

| Signal | Meaning (manual follow-up) |
|--------|----------------------------|
| **BUY** | Suggestion to **buy TQQQ** or **buy SQQQ** (symbol is stated on the alert) |
| **SELL** | Suggestion to **exit** the referenced ETF leg |
| **FLIP** | Suggestion to **switch** from TQQQ to SQQQ or vice versa |
| **CASH** | No new entry / hold commentary as applicable (includes explicit hold notes when already positioned) |

### What alerts may include (informational only)

Symbol, direction, entry zone, stop loss, take profit / stretch target, confidence, headline reasoning, and risk-oriented notes. These are **inputs for your judgment**, not instructions executed by this repo.

### Non-goals (never part of this project)

- No Robinhood or other **broker integration**
- No broker trading APIs or authenticated trading sessions
- No **automated order execution** or **automated position sizing that places orders**
- No real-money trade placement by the bot

Backtests and sweeps are **research simulations** on QQQ history only; they do not connect to a brokerage.

## Features

- Uses QQQ as analysis source across daily + 4h context
- Indicators: EMA(20/50), RSI(14), MACD, ATR(14), weekly VWAP, anchored VWAP, volume vs 20-period average
- **Weighted** bullish/bearish checklist (8 paired signals, configurable weights) with **0–100 strength** vs max stack
- **Regime-aware** thresholds (`trend_up` / `trend_down` / `range`) from daily EMA separation and slope vs ATR
- **Flip suppression** (minimum trading days in trade and/or extra opposite-side margin) to reduce whipsaws
- Normalized **confidence** (0–100 dominance of weighted stacks)
- Emits alerts: BUY, SELL, FLIP, CASH
- Risk logic: stop loss, take profit, stretch target, max hold
- Outputs: console, CSV journal, Discord webhook
- Structured JSON logs in `logs/bot.log` with daily rotation
- Dry-run mode for testing without Discord sends
- Optional `--backtest` mode for historical rule replay (QQQ directional proxy; see below)
- Discord **rich embeds** are mobile-first: strong BUY/SELL/FLIP/HOLD/CASH title, symbol/confidence/regime/strength, levels when relevant, short reason + leading checklist summary, optional paper-order outcome; full technical dump stays on the console (`--no-technical` to hide)
- Alpaca Market Data API backend (daily + 1h → synthetic 4h)
- Cloud scheduling via Render weekday crons; local durable learning via `scripts/run_weekday_paper.py` (autostart once with `bash scripts/install_paper_autostart.sh`)
- Read-only learner: `python main.py --learn-from-trades` (digest + gated proposals; no auto-apply)

## File layout

- `config.py`
- `event_calendar.py`
- `data.py`
- `indicators.py`
- `strategy_params.py`
- `strategy_scoring.py`
- `strategy.py`
- `alerts.py`
- `journal.py`
- `backtest.py`
- `backtest_sweep.py`
- `strategy_eval.py`
- `walk_forward.py`
- `learner.py` — read-only trade-log digest + gated proposals (`--learn-from-trades`)
- `main.py`
- `.env.example`
- `requirements.txt`
- `events.example.json`
- `position_state.example.json` (schema reference; runtime file `position_state.json` is gitignored)
- [`dashboard/`](./dashboard/) — optional **Next.js + Supabase** “Stock Signal Dashboard”: technical leaderboard (batch snapshots via [`publish_technical_dashboard.py`](./publish_technical_dashboard.py)) + news layer (research-only; see [`dashboard/README.md`](./dashboard/README.md))

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill values:

- `ALPACA_API_KEY` and `ALPACA_API_SECRET` (required)
- `ALPACA_DATA_BASE_URL=https://data.alpaca.markets` (optional override)
- `ALPACA_DATA_FEED=iex` (optional; use `sip` only with a paid Alpaca data plan)
- `DISCORD_WEBHOOK_URL` only required when `DRY_RUN=false`
- Optional **Alpaca paper trading** (off by default; never hits the live trading host):
  - `ALPACA_PAPER_TRADING=false` — set `true` **and** `DRY_RUN=false` to submit day **limit** orders on `https://paper-api.alpaca.markets` for BUY / SELL / FLIP alerts
  - `ALPACA_PAPER_NOTIONAL=500` — USD size per buy leg when equity % is unset; also the **cap** when volatility sizing is on
  - `ALPACA_PAPER_EQUITY_PCT=0` — if `> 0` and volatility sizing is off, buy size = paper equity × pct instead of fixed notional
  - `ALPACA_PAPER_LIMIT_OFFSET_BPS=10` — limit offset vs latest trade
  - `ALPACA_PAPER_VOL_SIZING=false` — when `true`, buy notional = equity × `ALPACA_PAPER_RISK_FRACTION` / stop distance, capped by `ALPACA_PAPER_NOTIONAL`. Off keeps the fixed notional / equity-% path
  - `ALPACA_PAPER_RISK_FRACTION=0.0075` — fraction of paper equity risked per buy (0.5–1% is the intended band; config allows up to 2%)
  - `ALPACA_PAPER_VOL_STOP=stop_pct` — `stop_pct` uses `STOP_LOSS_PCT`; `atr` uses `ALPACA_PAPER_ATR_STOP_MULT` × QQQ ATR / QQQ price
  - `EXIT_MODE=fixed` — `atr_trail` replaces the fixed take-profit exit with an ATR trail (`ATR_TRAIL_MULT`, default 2). `STRETCH_TAKE_PROFIT_PCT` is never an automatic exit
  - `ALPACA_TRADING_BASE_URL` defaults to the paper API; `https://api.alpaca.markets` is rejected at config load
  - Optional **paper risk controls** (default OFF so current Render soak is unchanged):
    - `PAPER_MAX_BUY_NOTIONAL=0` — when `> 0`, hard cap on buy notional (e.g. `200` for later live rehearsal)
    - `PAPER_TQQQ_ONLY=false` — when `true`, block SQQQ buys / flip-entries; sells still flatten
    - `PAPER_MAX_DAILY_LOSS_USD=0` / `PAPER_MAX_WEEKLY_LOSS_USD=0` — when `> 0`, kill switch trips on sleeve equity drop vs prior close / week-start; blocks new buys, posts Discord `KILL`
  - Separate from any Robinhood setup; see paper-trading notes below

Discord webhook quick setup:

1. In Discord, open **Server Settings -> Integrations -> Webhooks**.
2. Create webhook, choose channel, copy webhook URL.
3. Set `DISCORD_WEBHOOK_URL=<your webhook>` in `.env`.
4. Set `DRY_RUN=false` when you want live posts.

Optional event-risk avoidance (never fatal):

1. Copy `events.example.json` to `events.json`.
2. Maintain `blocked_dates` (YYYY-MM-DD) for days you never want **new** BUY entries while flat.
3. Optionally add a `risk_calendar` object with lists `cpi_dates`, `fomc_dates`, and `nasdaq_earnings_dates` (same date format).
4. Set `EVENT_RISK_AVOIDANCE=true` to merge those risk lists into the same entry-blocking set as `blocked_dates`.
5. Optionally set `EVENT_RISK_CALENDAR_URL` to fetch JSON containing a top-level `risk_calendar` object with the same keys. If the URL fails or returns invalid JSON, the bot logs `[events] ...` and continues with file-based dates only (or manual-only if the file has no usable data).

When `EVENT_RISK_AVOIDANCE` is