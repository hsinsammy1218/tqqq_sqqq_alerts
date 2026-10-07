-- Durable day/week/peak equity baselines for Robinhood host-mediated pilot.
-- Never fabricated from the current equity mark. Host merges RH previous-close
-- (when present) with these rows; missing baselines fail closed at risk time.

create table if not exists public.bot_robinhood_equity_baselines (
  bot_id text primary key,
  day_key date,
  day_start_equity double precision,
  week_key text,
  week_start_equity double precision,
  peak_equity double precision,
  detail text not null default '',
  updated_at timestamptz not null default now()
);

alter table public.bot_robinhood_equity_baselines enable row level security;

comment on table public.bot_robinhood_equity_baselines is
  'Durable RH host equity baselines (day/week/peak). No OAuth tokens. Fail closed when missing.';
