-- Atomic daily entry reservation for Alpaca live pilot.
-- INSERT wins; UNIQUE(bot_id, trading_day, entry_slot) is the concurrency authority.
-- Crash after reserve must NOT auto-free the slot.
-- Local JSON must not be the production concurrency authority for real money.

create table if not exists public.bot_live_entry_reservations (
  bot_id text not null default 'default',
  trading_day date not null,
  entry_slot int not null default 1,
  client_order_id text,
  purpose text,
  execution_symbol text,
  signal_id text,
  meta jsonb not null default '{}'::jsonb,
  reserved_at timestamptz not null default now(),
  primary key (bot_id, trading_day, entry_slot),
  constraint bot_live_entry_reservations_slot_positive check (entry_slot >= 1)
);

create index if not exists bot_live_entry_reservations_trading_day_idx
  on public.bot_live_entry_reservations (trading_day desc);

create index if not exists bot_live_entry_reservations_client_order_id_idx
  on public.bot_live_entry_reservations (client_order_id);

alter table public.bot_live_entry_reservations enable row level security;

comment on table public.bot_live_entry_reservations is
  'Atomic Alpaca live pilot daily entry slots. UNIQUE (bot_id, trading_day, entry_slot) is the concurrency authority. trading_day is America/New_York calendar date.';
