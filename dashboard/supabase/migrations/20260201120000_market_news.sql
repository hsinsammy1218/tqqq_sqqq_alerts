-- Market news layer (research dashboard). Run via Supabase CLI or SQL editor.

create extension if not exists "pgcrypto";

-- Canonical news articles (dedupe by normalized URL)
create table if not exists public.market_news (
  id uuid primary key default gen_random_uuid(),
  external_id text,
  headline text not null,
  summary text,
  source text not null default '',
  url text not null,
  published_at timestamptz not null,
  sentiment numeric,
  sentiment_model text,
  related_tickers text[] not null default '{}',
  related_sectors text[] not null default '{}',
  importance_score numeric not null default 50 check (importance_score >= 0 and importance_score <= 100),
  category text,
  raw_payload jsonb,
  created_at timestamptz not null default now(),
  constraint market_news_url_unique unique (url)
);

create index if not exists idx_market_news_published_at on public.market_news (published_at desc);
create index if not exists idx_market_news_category on public.market_news (category);
create index if not exists idx_market_news_tickers on public.market_news using gin (related_tickers);
create index if not exists idx_market_news_sectors on public.market_news using gin (related_sectors);

-- Ingest audit / ops
create table if not exists public.news_ingest_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',
  provider text not null default '',
  items_fetched int not null default 0,
  items_inserted int not null default 0,
  items_deduped int not null default 0,
  error_message text,
  created_at timestamptz not null default now()
);

create index if not exists idx_news_ingest_runs_started on public.news_ingest_runs (started_at desc);

comment on table public.market_news is 'Research-only news context; does not drive technical signals.';
comment on table public.news_ingest_runs is 'Audit trail for scheduled news ingest jobs.';
