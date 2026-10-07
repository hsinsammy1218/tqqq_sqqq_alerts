-- Atomic daily entry reservation for Robinhood host-mediated pilot.
-- UNIQUE(bot_id, trading_day, entry_slot). Insert wins; unique violation → BLOCK.

create table if not exists public.bot_robinhood_entry_reservations (
  bot_id text not null default 'default',
  trading_day date not null,
  entry_slot integer not null default 1,
  client_order_id text,
  purpose text,
  execution_symbol text,
  signal_id text,
  meta jsonb not null default '{}'::jsonb,
  reserved_at timestamptz not null default now(),
  primary key (bot_id, trading_day, entry_slot),
  constraint bot_rh_entry_slot_positive check (entry_slot >= 1)
);

alter table public.bot_robinhood_entry_reservations enable row level security;

comment on table public.bot_robinhood_entry_reservations is
  'One new Robinhood host-pilot entry per bot/trading day/slot. Crash after reserve does not auto-free.';
