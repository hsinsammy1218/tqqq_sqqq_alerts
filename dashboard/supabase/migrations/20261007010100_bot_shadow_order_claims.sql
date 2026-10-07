-- Idempotency claims for Alpaca live-shadow intents.
-- Unique client_order_id prevents double-propose after a crash.
-- execution_status remains NOT_SUBMITTED in Phase 3.

create table if not exists public.bot_shadow_order_claims (
  client_order_id text primary key,
  bot_id text not null default 'default',
  broker text not null default 'alpaca',
  execution_mode text not null default 'live_shadow',
  execution_status text not null default 'NOT_SUBMITTED',
  action text,
  execution_symbol text,
  purpose text,
  claimed_at timestamptz not null default now(),
  constraint bot_shadow_order_claims_not_live check (execution_status = 'NOT_SUBMITTED'),
  constraint bot_shadow_order_claims_mode check (execution_mode = 'live_shadow')
);

create index if not exists bot_shadow_order_claims_bot_id_idx
  on public.bot_shadow_order_claims (bot_id);

alter table public.bot_shadow_order_claims enable row level security;

comment on table public.bot_shadow_order_claims is
  'Alpaca live-shadow client_order_id claims. Not a live order journal.';
