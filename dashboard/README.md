# Stock Signal Dashboard (news layer)

Next.js App Router + Supabase research UI for **market news context**. Technical scoring stays unchanged; news is confirmation / risk / catalyst context only. No broker APIs or execution.

## Setup

1. Create a Supabase project and run the SQL migration:

   - [`supabase/migrations/20260201120000_market_news.sql`](./supabase/migrations/20260201120000_market_news.sql)

2. Copy env:

   ```bash
   cp .env.example .env.local
   ```

   Fill `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `NEWS_API_KEY` (Finnhub), and `CRON_SECRET`.

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

## Routes

| Path | Purpose |
|------|---------|
| `/` | Dashboard + headline panel |
| `/market-news` | Paginated market feed |
| `/stock/[ticker]` | Ticker-tagged news + narrative-risk / catalyst cues |
| `/performance` | Placeholder — wire to your analytics tables |
| `/api/news/ingest` | Secured ingest (GET/POST + secret) |
| `/api/news/market` | JSON list (`page`, `hours`, `sector`, `limit`) |
| `/api/news/ticker/[symbol]` | JSON list for one symbol |

## Security

- Never expose `NEWS_API_KEY` or `SUPABASE_SERVICE_ROLE_KEY` to the client.
- Ingest requires `CRON_SECRET` (Bearer or `x-cron-secret` header).

## Provider

V1 uses **Finnhub** (`NEWS_PROVIDER=finnhub`). Replace the adapter in [`lib/news/providers/`](./lib/news/providers/) to add Polygon/Benzinga later.
