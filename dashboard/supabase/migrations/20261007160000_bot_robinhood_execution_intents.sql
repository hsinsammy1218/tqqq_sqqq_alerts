-- Durable Robinhood host-mediated execution intents.
-- Render may INSERT PENDING rows. Only an authenticated MCP host executor
-- may CLAIM and advance toward submission. No OAuth tokens are stored.

create table if not exists public.bot_robinhood_execution_intents (
  id uuid primary key default gen_random_uuid(),
  client_order_id text not null unique,
  bot_id text not null default 'default',
  status text not null default 'PENDING',
  strategy_version text not null,
  signal_symbol text not null,
  execution_symbol text not null,
  action text not null,
  quantity integer not null,
  estimated_price double precision,
  estimated_notional double precision,
  confidence integer,
  regime text,
  reason text,
  signal_id text not null,
  purpose text not null,
  intent_timestamp timestamptz not null,
  expires_at timestamptz not null,
  integrity_digest text not null,
  broker_order_id text,
  filled_qty double precision,
  risk_status text,
  risk_reason text,
  detail text,
  claimed_at timestamptz,
  claimed_by text,
  submitted_at timestamptz,
  terminal_at timestamptz,
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint bot_rh_exec_intents_status check (
    status in (
      'PENDING',
      'CLAIMED',
      'SUBMITTED',
      'FILLED',
      'PARTIALLY_FILLED',
      'REJECTED',
      'CANCELLED',
      'EXPIRED',
      'UNKNOWN',
      'BLOCKED',
      'EXPIRED_INTENT',
      'SUPERSEDED'
    )
  ),
  constraint bot_rh_exec_intents_action check (action in ('BUY', 'SELL')),
  constraint bot_rh_exec_intents_symbol check (execution_symbol in ('TQQQ', 'SQQQ')),
  constraint bot_rh_exec_intents_signal check (signal_symbol = 'QQQ'),
  constraint bot_rh_exec_intents_qty check (quantity >= 0)
);

create index if not exists bot_rh_exec_intents_status_expires_idx
  on public.bot_robinhood_execution_intents (status, expires_at);

create index if not exists bot_rh_exec_intents_bot_created_idx
  on public.bot_robinhood_execution_intents (bot_id, created_at desc);

alter table public.bot_robinhood_execution_intents enable row level security;

comment on table public.bot_robinhood_execution_intents is
  'Host-mediated Robinhood TradeIntents. Render creates PENDING; MCP host claims/executes. No OAuth tokens.';
