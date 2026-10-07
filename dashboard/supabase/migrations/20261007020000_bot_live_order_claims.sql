-- Atomic client_order_id claims for Alpaca live pilot (and durable shadow).
-- INSERT wins; unique violation means another writer already claimed the id.
-- Local JSON must not be the production concurrency authority for real money.

create table if not exists public.bot_live_order_claims (
  client_order_id text primary key,
  bot_id text not null default 'default',
  broker text not null default 'alpaca',
  execution_mode text not null,
  execution_status text not null default 'CLAIMED',
  action text,
  execution_symbol text,
  purpose text,
  meta jsonb not null default '{}'::jsonb,
  claimed_at timestamptz not null default now(),
  constraint bot_live_order_claims_mode check (
    execution_mode in ('live_shadow', 'live_pilot')
  )
);

create index if not exists bot_live_order_claims_bot_id_idx
  on public.bot_live_order_claims (bot_id);

create index if not exists bot_live_order_claims_claimed_at_idx
  on public.bot_live_order_claims (claimed_at desc);

alter table public.bot_live_order_claims enable row level security;

comment on table public.bot_live_order_claims is
  'Atomic Alpaca live client_order_id claims. Unique PK is the concurrency authority.';
