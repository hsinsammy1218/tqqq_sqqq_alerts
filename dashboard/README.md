# Stock Signal Dashboard

Next.js App Router + Supabase research UI: **technical leaderboard** (batch snapshots from the Python publisher) plus **market news context**. News never changes technical scores. No broker APIs or execution.

## Setup

1. Create a Supabase project and run the SQL migrations (in order):

   - [`supabase/migrations/20260201120000_market_news.sql`](./supabase/migrations/20260201120000_market_news.sql)
   - [`supabase/migrations/20260202120000_technical_snapshots.sql`](./supabase/migrations/20260202120000_technical_snapshots.sql)
   - [`supabase/migrations/20260714180000_bot_position_state.sql`](./supabase/migrations/20260714180000_bot_position_state.sql) (alert bot cloud position memory)

2. Copy env:

   ```bash
   cp .env.example .env.local
   ```

   Fill `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `NEWS_API_KEY` (Finnhub), and auth config:
   - `CRON_SECRET` (legacy shared secret), or
   - `ROUTE_AUTH_TOKENS_JSON` (scoped route tokens), or both during migration.

3. Install and dev:

   ```bash
   npm install
   npm run dev
   ```

## Ingest (server-only)

```bash
curl -X POST -H "Authorization: Bearer YOUR_CRON_SECRET" http://localhost:3000/api/news/ingest
```

Vercel Cron (see [`vercel.json`](./vercel.json)) sends **GET** with `Authorization: Bearer CRON_SECRET` when `CRON_SECRET` is set in the project — both GET and POST are supported.

Scoped auth option:

```env
ROUTE_AUTH_TOKENS_JSON=[{"token":"news-token","scopes":["news:ingest"],"principal":"vercel-cron"}]
```

Supported scopes:
- `news:ingest` for `/api/news/ingest`
- `technical:revalidate` for `/api/technical/revalidate`

## Technical leaderboard (batch job)

From the **repository root** (not `dashboard/`), with the same `.env` as the alert bot plus Supabase variables:

```bash
pip install -r requirements.txt
python publish_technical_dashboard.py --dry-run   # list TECHNICAL_UNIVERSE symbols
python publish_technical_dashboard.py            # fetch via ka, score, insert dashboard_runs + stock_snapshots
```

Requires `KLICKANALYTICS_CLI_API_KEY`, `SUPABASE_URL` (or `NEXT_PUBLIC_SUPABASE_URL`), `SUPABASE_SERVICE_ROLE_KEY`, and optionally `TECHNICAL_UNIVERSE` (comma-separated tickers).

Optional: `DASHBOARD_BASE_URL` + `CRON_SECRET` so the publisher POSTs `/api/technical/revalidate` and clears the Next.js cache tag `technical`.

## Routes

| Path | Purpose |
|------|---------|
| `/` | Dashboard + technical leaderboard (compact) + headline panel |
| `/leaderboard` | Full technical leaderboard table |
| `/market-news` | Paginated market feed |
| `/stock/[ticker]` | Technical breakdown + ticker-tagged news + narrative-risk / catalyst cues |
| `/performance` | Placeholder — wire to your analytics tables |
| `/api/news/ingest` | Secured ingest (GET/POST + secret) |
| `/api/news/market` | JSON list (`page`, `hours`, `sector`, `limit`) |
| `/api/news/ticker/[symbol]` | JSON list for one symbol |
| `/api/technical/revalidate` | POST + CRON_SECRET — `revalidateTag('technical')` |

## Security

- Never expose `NEWS_API_KEY` or `SUPABASE_SERVICE_ROLE_KEY` to the client.
- News ingest and `/api/technical/revalidate` require auth via:
  - `CRON_SECRET` (Bearer or `x-cron-secret`), or
  - scoped token via `ROUTE_AUTH_TOKENS_JSON`.

## Provider

V1 uses **Finnhub** (`NEWS_PROVIDER=finnhub`). Replace the adapter in [`lib/news/providers/`](./lib/news/providers/) to add Polygon/Benzinga later.
