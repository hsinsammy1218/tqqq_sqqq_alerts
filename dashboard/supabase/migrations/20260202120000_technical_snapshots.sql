-- Technical snapshots for research dashboard (batch-computed; not driven by news).

create extension if not exists "pgcrypto";

create table if not exists public.dashboard_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',
  error_message text,
  tickers_requested int not null default 0 check (tickers_requested >= 0),
  tickers_succeeded int not null default 0 check (tickers_succeeded >= 0),
  scorer_version text not null default '',
  params_json jsonb not null default '{}'::jsonb,
  source text not null default 'python_batch',
  constraint dashboard_runs_status_chk check (status in ('running', 'ok', 'error'))
);

create index if not exists idx_dashboard_runs_started on public.dashboard_runs (started_at desc);

create table if not exists public.stock_snapshots (
  id uuid primary key default gen_random_uuid(),
  dashboard_run_id uuid not null references public.dashboard_runs (id) on delete cascade,
  symbol text not null,
  computed_at timestamptz not null default now(),
  daily_bar_end timestamptz,
  h4_bar_end timestamptz,
  regime text not null,
  bull_strength int not null,
  bear_strength int not null,
  confidence int not null,
  breakdown_json jsonb not null default '{}'::jsonb,
  constraint stock_snapshots_symbol_nonempty check (length(trim(symbol)) > 0),
  constraint stock_snapshots_bull_chk check (bull_strength >= 0 and bull_strength <= 100),
  constraint stock_snapshots_bear_chk check (bear_strength >= 0 and bear_strength <= 100),
  constraint stock_snapshots_conf_chk check (confidence >= 0 and confidence <= 100),
  constraint stock_snapshots_run_symbol_unique unique (dashboard_run_id, symbol)
);

create index if not exists idx_stock_snapshots_run on public.stock_snapshots (dashboard_run_id);
create index if not exists idx_stock_snapshots_symbol on public.stock_snapshots (symbol);
create index if not exists idx_stock_snapshots_run_confidence on public.stock_snapshots (dashboard_run_id, confidence desc);

comment on table public.dashboard_runs is 'Batch metadata for technical snapshot publishes (Python job).';
comment on table public.stock_snapshots is 'Deterministic checklist scores per symbol per run; research-only.';
comment on column public.stock_snapshots.confidence is 'Stack dominance 0-100 (normalized_confidence_score); primary leaderboard sort.';
comment on column public.stock_snapshots.breakdown_json is 'schema_version + indicator_snapshot + checklist reasons; see publisher script.';
