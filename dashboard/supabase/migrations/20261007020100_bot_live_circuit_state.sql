-- Durable circuit-breaker and one-entry-per-day state for Alpaca live pilot.

create table if not exists public.bot_live_circuit_state (
  bot_id text primary key,
  day_key date,
  entries_today int not null default 0,
  week_key text,
  kill_new_entries boolean not null default false,
  daily_loss_tripped boolean not null default false,
  weekly_loss_tripped boolean not null default false,
  drawdown_tripped boolean not null default false,
  detail text not null default '',
  updated_at timestamptz not null default now()
);

alter table public.bot_live_circuit_state enable row level security;

comment on table public.bot_live_circuit_state is
  'Durable Alpaca live pilot circuit breakers and daily entry cap.';
