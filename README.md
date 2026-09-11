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
- Discord **rich embeds** match the console breakdown: bot memory (flat vs symbol), bar timestamps + blackout, full QQQ daily/4h indicator lines, every bull/bear checklist item, rule thresholds, hold exit flags when applicable, then notes
- KlickAnalytics CLI market data backend (default), with pluggable **Polygon.io** and **Yahoo Finance** providers via `MARKET_DATA_PROVIDER`
- Cloud scheduling via Render (worker/cron) — local Windows Task Scheduler is not used

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
- `walk_forward.py`
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

- `MARKET_DATA_PROVIDER` — `klickanalytics` (default), `polygon`, or `yahoo`
  ([provider comparison](https://www.timestored.com/data/realtime-stock-data-apis))
- `KLICKANALYTICS_CLI_API_KEY` (required when provider is `klickanalytics`)
- `KLICKANALYTICS_CLI_COMMAND=ka` (override only if your executable name/path differs)
- `POLYGON_API_KEY` (required when provider is `polygon`; `MASSIVE_API_KEY` also accepted)
- `DISCORD_WEBHOOK_URL` only required when `DRY_RUN=false`

**Provider guidance (QQQ daily + hourly → 4h context):**

| Provider | Cost fit | Notes |
|----------|----------|-------|
| `yahoo` | Free | Fast unblock when Klick monthly quota is exhausted; ~60d of 1h history (fine for live alerts, thin for deep backtests) |
| `polygon` | ~$29/mo Starter | Best paid match from the comparison page: unlimited REST aggregates, hourly bars, websockets available. Free Basic is EOD-only (no hourly). |
| `klickanalytics` | Existing | Keep if quota allows; ~2 CLI calls per poll burns a 500/mo plan quickly |

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

When `EVENT_RISK_AVOIDANCE` is false or no risk data is present, behavior matches manual `blocked_dates` only. Missing `events.json` is still OK (no blackout dates).

Optional strategy tuning (baseline **5/5** entry, **3 weak** with equal weights; chop/range and flip behavior tuned via rows below):

| Variable | Meaning |
|----------|---------|
| `SCORE_WEIGHTS` | Eight comma-separated weights for the checklist items (daily→4h→VWAP→volume order); empty = all `1` |
| `REGIME_SEP_ATR_MULT` | \|(EMA20−EMA50)\|/ATR threshold for trending vs chop |
| `REGIME_SLOPE_ATR_MULT` | Daily EMA20 one-bar slope / ATR threshold |
| `REGIME_RANGING_THRESHOLD_WEIGHT_ADD` | Extra weighted hurdle for **both** sides in `range` regime |
| `REGIME_TREND_FAVORABLE_DELTA` | Weight-units easier/harder entry along vs against the trend |
| `FLIP_MIN_HOLD_TRADING_DAYS` | Full weekdays after entry before an opposite flip is allowed without extra margin |
| `FLIP_MARGIN_WEIGHT` | Opposite stack must exceed its threshold by this many weight-units to flip early |
| `ENTRY_DOMINANCE_GAP_WEIGHT` | Minimum weighted bull−bear separation for dominance fallback entry after strict gates fail (`0` disables) |
| `FLIP_ALLOW_IN_RANGE` | When `false` (default), **no FLIP on reversal** during `range` chop — exit via weaken / stop / TP / max hold only; set `true` to allow reversal flips in range |
| `MIN_CONFIDENCE_TO_TRADE` | Minimum normalized confidence (0–100) for **flat** BUY TQQQ/SQQQ; weaker dominance → **CASH** (`0` disables). Repo default **62** matches walk-forward rank 1 (`reports/walk_forward_results.csv`). |

## Run

Dry-run:

```bash
python main.py --dry-run
```

Optional **stricter flat entries**: require normalized confidence ≥75% (still after `MIN_CONFIDENCE_TO_TRADE`):

```bash
python main.py --dry-run --high-confidence-only
```

Same flag applies to `--backtest`, `--backtest-sweep`, and `--walk-forward` research runs.

Set logging verbosity (default `INFO`):

```bash
python main.py --log-level DEBUG --dry-run
```

Run non-destructive environment checks:

```bash
python main.py --health-check --dry-run
```

Health check verifies:
- KlickAnalytics CLI command availability
- Market data fetch success (and latest daily/4h bar timestamps)
- `position_state.json` loadability (or safe recovery to flat with warnings)
- `events.json` readability if present
- Discord webhook configuration when not in dry-run mode

## Deploy on Render

Render Cron containers are **ephemeral** — local `position_state.json` does not survive between runs. Use **Supabase** for position memory.

1. **Run the migration** in your Supabase SQL editor (same project as the dashboard):

   [`dashboard/supabase/migrations/20260714180000_bot_position_state.sql`](./dashboard/supabase/migrations/20260714180000_bot_position_state.sql)

2. **Create an Environment Group** in Render named `tqqq-sqqq-alerts` (matches [`render.yaml`](./render.yaml)) with at least:

   | Key | Value |
   |-----|--------|
   | `KLICKANALYTICS_CLI_API_KEY` | your CLI key |
   | `DISCORD_WEBHOOK_URL` | webhook URL |
   | `DRY_RUN` | `false` |
   | `POSITION_STATE_BACKEND` | `supabase` |
   | `POSITION_STATE_BOT_ID` | `default` (or another id if you run multiple bots) |
   | `SUPABASE_URL` | project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | service role key |

   Copy any other strategy knobs from your local `.env` as needed.

3. **Deploy the Blueprint**: Dashboard → New → Blueprint → this repo (`render.yaml`). That creates three weekday crons (EDT / UTC−4):

   | Service | Local Eastern | UTC cron |
   |---------|---------------|----------|
   | `tqqq-sqqq-alerts-1000` | 10:00 | `0 14 * * 1-5` |
   | `tqqq-sqqq-alerts-1230` | 12:30 | `30 16 * * 1-5` |
   | `tqqq-sqqq-alerts-1530` | 15:30 | `30 19 * * 1-5` |

   Start command: `python main.py --no-technical --market-hours-only`.

4. **Seed position** (once) from a machine with the same Supabase env (`POSITION_STATE_BACKEND=supabase` plus URL and service role). These flags **save memory and exit** — they do not fetch data or send Discord:

   ```bash
   python main.py --set-position TQQQ --entry-price 72.50
   # or
   python main.py --flat
   ```

   Do not add `--set-position` / `--flat` to the Render cron start command (that would skip the alert run). After seeding, weekday crons pick up the row on their own.

**DST note:** Render cron expressions are UTC. The schedules above assume Eastern Daylight (UTC−4). In Eastern Standard (UTC−5), shift each hour +1, or leave as-is and rely on `--market-hours-only` (jobs may skip or run near the edge of the session).

**Journal / usage JSON** on Render are best-effort only (ephemeral disk). Discord is sent **before** position is persisted so a webhook failure can retry on the next cron. Discord remains the durable alert channel.

### Backtest (optional)

Replays the same scoring and `decide()` rules over recent historical QQQ daily bars (with expanding daily + 4h context). Useful for sanity-checking how often BUY / SELL / FLIP would have fired before trusting live alerts.

**Does not** post Discord, append the CSV journal, or modify `position_state.json`.

**Caveat — QQQ directional proxy only:** reported equity applies QQQ close-to-close moves as a stand-in (long TQQQ ~ positive QQQ return, long SQQQ ~ negative QQQ return). It ignores leveraged ETF mechanics, borrow/fees, spreads, and partial fills. Treat results as rule-frequency / rough regime checks, not predictive performance.

```bash
python main.py --backtest
python main.py --backtest --backtest-bars 250
python main.py --backtest --backtest-bars 250 --backtest-report-csv reports/backtest_trades.csv
```

Parameter sweep (grid search; **research only** — uses the same `--backtest-bars` window):

```bash
python main.py --backtest-sweep --backtest-bars 250
python main.py --backtest-sweep --backtest-bars 250 --backtest-sweep-csv reports/sweep_results.csv
```

Sweep mode is for comparing parameter combinations offline. Results use the **QQQ directional proxy** (not real ETF fills). **Best-ranked settings are not guaranteed future performance.**

Define grids via comma-separated env vars (omit a variable to keep only its single value from your normal `.env`):

| Env | Swept parameter |
|-----|-----------------|
| `BACKTEST_SWEEP_BULL` | `BULL_ENTRY_THRESHOLD` |
| `BACKTEST_SWEEP_BEAR` | `BEAR_ENTRY_THRESHOLD` |
| `BACKTEST_SWEEP_WEAK` | `WEAK_SCORE_THRESHOLD` |
| `BACKTEST_SWEEP_RANGE_ADD` | `REGIME_RANGING_THRESHOLD_WEIGHT_ADD` |
| `BACKTEST_SWEEP_FLIP_HOLD` | `FLIP_MIN_HOLD_TRADING_DAYS` |
| `BACKTEST_SWEEP_FLIP_MARGIN` | `FLIP_MARGIN_WEIGHT` |
| `BACKTEST_SWEEP_DOM_GAP` | `ENTRY_DOMINANCE_GAP_WEIGHT` |
| `BACKTEST_SWEEP_MIN_CONFIDENCE` | `MIN_CONFIDENCE_TO_TRADE` |

Legacy aliases (`BACKTEST_SWEEP_BULL_ENTRY_THRESHOLD`, `BACKTEST_SWEEP_BEAR_ENTRY_THRESHOLD`, etc.) are still read if the short name is unset (`BACKTEST_SWEEP_MIN_CONFIDENCE_TO_TRADE` for confidence).

Example grid:

```env
BACKTEST_SWEEP_BULL=3,4,5
BACKTEST_SWEEP_BEAR=3,4,5
BACKTEST_SWEEP_WEAK=2,3
BACKTEST_SWEEP_RANGE_ADD=0.0,0.25,0.5
BACKTEST_SWEEP_FLIP_HOLD=0,1,2
BACKTEST_SWEEP_FLIP_MARGIN=0.0,0.5,1.0
BACKTEST_SWEEP_MIN_CONFIDENCE=55,60,65,70,75
```

Console shows the **top 10** combinations by **balanced score** (`compute_balanced_score` in `backtest_sweep.py`):

`balanced_score` (see `compute_balanced_score` in `backtest_sweep.py`): combines total return, win rate (×0.22), average per-trade return (×0.18), median per-trade return when trades exist (×0.12), penalties for drawdown (×1.55) and flips (×0.42), plus mild penalties when trades fall below 6 or above 42 (anti-underfitting / anti-overtrading).

`--backtest-sweep-csv` writes **one row per combination** with full parameters and metrics (complete ranked list).

If both `--backtest` and `--backtest-sweep` are passed, **sweep wins** (single backtest is skipped).

`--walk-forward` cannot be combined with `--backtest` or `--backtest-sweep`.

### Walk-forward parameter selection (optional)

Splits the same `--backtest-bars` trailing window into chronological validation folds (default **3**, `WALK_FORWARD_FOLDS`). For each grid combination of the knobs below, runs a sliced backtest per fold, computes `compute_balanced_score` per fold, and ranks combinations by the **mean** validation score. Console output summarizes the **recommended** env-style settings plus pooled trade count, win rate, median return/trade, worst-fold max drawdown, and fold count. Results still use the **QQQ directional proxy** only.

```bash
python main.py --walk-forward --backtest-bars 250
python main.py --walk-forward --backtest-bars 250 --walk-forward-csv reports/walk_forward_results.csv
```

`--walk-forward-csv` defaults to `reports/walk_forward_results.csv`. Leaving normal `.env` defaults unchanged is intentional until you adopt a recommendation manually.

| Env | Parameter |
|-----|-----------|
| `WALK_FORWARD_GRID_MIN_CONFIDENCE` | `MIN_CONFIDENCE_TO_TRADE` |
| `WALK_FORWARD_GRID_DOM_GAP` | `ENTRY_DOMINANCE_GAP_WEIGHT` |
| `WALK_FORWARD_GRID_RANGE_ADD` | `REGIME_RANGING_THRESHOLD_WEIGHT_ADD` |
| `WALK_FORWARD_GRID_FLIP_HOLD` | `FLIP_MIN_HOLD_TRADING_DAYS` |
| `WALK_FORWARD_GRID_FLIP_MARGIN` | `FLIP_MARGIN_WEIGHT` |
| `WALK_FORWARD_GRID_FLIP_ALLOW_IN_RANGE` | `FLIP_ALLOW_IN_RANGE` (`true`/`false`, comma-separated) |

Example:

```env
WALK_FORWARD_FOLDS=3
WALK_FORWARD_GRID_MIN_CONFIDENCE=55,62,70
WALK_FORWARD_GRID_DOM_GAP=1.0,1.25
WALK_FORWARD_GRID_RANGE_ADD=0.55,0.65
WALK_FORWARD_GRID_FLIP_HOLD=3
WALK_FORWARD_GRID_FLIP_MARGIN=1.25,1.5
WALK_FORWARD_GRID_FLIP_ALLOW_IN_RANGE=false,true
```

Backtest report now includes:
- total trades
- win rate
- average return per trade
- median return per trade (closed legs only)
- best trade / worst trade
- max drawdown
- average hold days
- FLIP event count
- CASH no-trade period count

`--backtest-report-csv` exports per-trade rows with:
`timestamp,action,symbol,entry_price,exit_price,return_pct,hold_days,bull_strength,bear_strength,confidence,regime,reason`

Live Discord alerts:

```bash
python main.py
```

After the action line, the bot prints a **technical breakdown**: last bar timestamps, full QQQ daily/4h indicator snapshot, every bull/bear score check that fired, threshold rules, position memory, and (when relevant) hold/exit flags vs stop/TP/max-hold. For a short console log only, use `python main.py --no-technical`.

Tell the bot you have **no brokerage position** (sync bot memory to flat):

```bash
python main.py --flat
```

`--flat` and `--set-position` **save bot memory and exit**. They do not fetch data, decide, or send Discord. Use `--flat` once after you’ve closed everything on Robinhood (or on first setup). The next **normal** run (`python main.py`) still updates position memory if a BUY signal appears.

Tell the bot you **already hold** TQQQ or SQQQ (e.g. you bought on Robinhood before the bot tracked it):

```bash
python main.py --set-position TQQQ
python main.py --set-position TQQQ --entry-price 72.50
python main.py --set-position SQQQ --entry-price 14.20 --entry-time 2026-05-01T14:30:00Z
```

Only **which symbol** you hold is required. **`--entry-price`** (your ETF average cost) and **`--entry-time`** are optional: if you skip price, stop/target math uses **QQQ’s daily close** as a stand-in; if you skip time, **max hold** starts from the **first bot run** that finishes after this sync.

Persistent position memory defaults to `position_state.json` (path via `POSITION_STATE_JSON`). On Render (or any ephemeral host), set `POSITION_STATE_BACKEND=supabase` so the same fields live in `public.bot_position_state` (row key `POSITION_STATE_BOT_ID`). The bot writes canonical keys:

| Field | Meaning |
|-------|---------|
| `symbol` | `"TQQQ"`, `"SQQQ"`, or `null` when flat |
| `entry_price` | ETF average cost or `null` (QQQ close proxy for exits) |
| `entry_time` | ISO timestamp when opened or `null` |
| `last_signal` | Last alert-side transition (`BUY`, `SELL`, `FLIP`, `CASH`, `MANUAL_SET`, `MANUAL_FLAT`, …) |
| `updated_at` | UTC ISO8601 when this record was last saved |

Legacy keys `active_symbol` / `entry_timestamp` are still read on load for backward compatibility.

Manual sync examples match [`position_state.example.json`](position_state.example.json):

```json
{
  "symbol": "TQQQ",
  "entry_price": 72.5,
  "entry_time": "2026-05-01T14:30:00Z",
  "last_signal": "MANUAL_SET",
  "updated_at": "2026-05-01T16:00:00Z"
}
```

Flat recovery (after closing everything at the broker):

```json
{
  "symbol": null,
  "entry_price": null,
  "entry_time": null,
  "last_signal": "MANUAL_FLAT",
  "updated_at": "2026-05-01T16:00:00Z"
}
```

If the **file** backend is missing, unreadable JSON, or has an invalid `symbol`, the bot prints `[position] …` and **starts flat** (never crashes). The **Supabase** backend fail-closes instead: API errors and invalid rows abort the run so a corrupt cloud row cannot be treated as flat. To recover a file: delete `position_state.json`, run `python main.py --flat`, or paste a valid JSON object from the example.

Use `null` for `entry_price` / `entry_time` when you only want the bot to know the side. Prefer `python main.py --flat` / `--set-position` over hand-editing when possible.

## Decision rules

Legacy knobs `BULL_ENTRY_THRESHOLD`, `BEAR_ENTRY_THRESHOLD`, `WEAK_SCORE_THRESHOLD` stay on a **0–8 scale**; internally they map to **weighted-sum targets** (same logic as before when every weight is `1`).

- BUY TQQQ when **weighted bull sum ≥ effective bull target**, **weighted bear sum < effective bear target**, flat, not blocked.
- BUY SQQQ when **weighted bear sum ≥ effective bear target**, **weighted bull sum < effective bull target**, flat, not blocked.
- Flat BUY additionally requires **normalized confidence ≥ `MIN_CONFIDENCE_TO_TRADE`** (below → CASH). **`--high-confidence-only`** raises that cutoff to **75%**.
- **Effective targets** shift by regime (stricter in `range`, slightly easier with the trend in `trend_up` / `trend_down`).
- FLIP when the opposite side clears its effective threshold **and** flip-suppression rules pass (otherwise HOLD with a note).
- CASH when mixed/weak/conflicting while flat, or HOLD messaging while in a position without exit/flip.
- SELL when weak vs weighted threshold, stop/TP, max hold, etc.

Mutual exclusivity is enforced: never hold TQQQ and SQQQ simultaneously.

## Sample Discord alert

```text
ACTION: BUY TQQQ
(You trade manually - alerts only, no broker execution.)

Alert: BUY
Symbol: TQQQ
QQQ trend: daily close > EMA20; daily EMA20 > EMA50; 4h EMA20 > EMA50 | regime=trend_up
Bullish strength: 75/100 (weighted checklist)
Bearish strength: 25/100 (weighted checklist)
Confidence (normalized): 78%
Signal quality: HIGH
Entry zone: 432.10 - 439.70
Stop loss: 400.65
Take profit: 502.13
Stretch target: 545.79
Max hold date: 2026-05-15
Timestamp: 2026-05-01T14:20:00Z
Notes: Bullish QQQ setup.
```

Flat **BUY** alerts include **Signal quality**: **HIGH** when normalized confidence ≥75%, **MEDIUM** when ≥`MIN_CONFIDENCE_TO_TRADE` and below 75%. Non-BUY alerts omit the line.

## Sample CSV journal format

```csv
timestamp_utc,alert_type,execution_symbol,qqq_trend_reason,bull_score,bear_score,confidence,entry_zone_low,entry_zone_high,stop_price,take_profit_price,take_profit_stretch_price,max_hold_date,notes,signal_quality
2026-05-01T14:20:00Z,BUY,TQQQ,"daily close > EMA20; daily EMA20 > EMA50 | regime=trend_up",75,25,78,432.1,439.7,400.65,502.13,545.79,2026-05-15,Bullish QQQ setup.,HIGH
```

Older journal files without the `signal_quality` column may misalign if appended after upgrading; rotate or archive the CSV if needed.

## Notes

- 4h candles are synthesized from 1h KlickAnalytics intraday bars.
- Data is pulled via KlickAnalytics CLI commands (`ka prices` and `ka intraday`).
- Real-time quality depends on your KlickAnalytics plan and your run cadence.
- Educational use only. Not financial advice.
