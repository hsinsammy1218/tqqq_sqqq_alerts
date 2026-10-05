-- Shadow intents only. execution_status stays NOT_SUBMITTED in this phase.
-- Not a live broker order journal. Apply after bot_trade_log if you want
-- Supabase copies; the bot also appends logs/robinhood_shadow.jsonl.

create table if not exists public.bot_shadow_orders (
  id uuid primary key default gen_random_uuid(),
  bot_id text not null default 'default',
  "timestamp" text not null,
  strategy_version text,
  signal_id text,
  broker text not null default 'robinhood',
  execution_mode text not null default 'shadow',
  signal_symbol text,
  execution_symbol text,
  action text,
  quantity integer,
  estimated_price double precision,
  estimated_notional double precision,
  confidence integer,
  regime text,
  risk_status text,
  risk_reason text,
  broker_state text,
  execution_status text not null default 'NOT_SUBMITTED',
  client_order_id text,
  created_at timestamptz not null default now(),
  constraint bot_shadow_orders_not_live check (execution_status = 'NOT_SUBMITTED'),
  constraint bot_shadow_orders_shadow_mode check (execution_mode = 'shadow')
);

create index if not exists bot_shadow_orders_client_order_id_idx
  on public.bot_shadow_orders (client_order_id);

alter table public.bot_shadow_orders enable row level security;

comment on table public.bot_shadow_orders is
  'Robinhood shadow intents. Rows are would-be orders, never live fills.';
