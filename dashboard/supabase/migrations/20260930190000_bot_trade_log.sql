-- Append-only paper trade journal for cloud runners (Render Cron).
-- Same logical fields as logs/trades.jsonl; service role only.
-- Apply in Supabase SQL editor after bot_position_state migration.

create table if not exists public.bot_trade_log (
  id uuid primary key default gen_random_uuid(),
  bot_id text not null default 'default',
  "timestamp" text not null,
  symbol text,
  side text,
  qty text,
  limit_price text,
  fill_price text,
  filled_qty text,
  order_id text,
  status text,
  purpose text,
  alert_type text,
  confidence integer,
  regime text,
  signal_quality text,
  paper_trading boolean,
  dry_run boolean,
  error text,
  source text,
  detail text,
  created_at timestamptz not null default now()
);

create index if not exists bot_trade_log_bot_id_timestamp_idx
  on public.bot_trade_log (bot_id, "timestamp");

create index if not exists bot_trade_log_bot_id_order_id_idx
  on public.bot_trade_log (bot_id, order_id)
  where order_id is not null;

comment on table public.bot_trade_log is
  'Append-only Alpaca paper trade journal for the Python alert bot (durable alternative to ephemeral logs/trades.jsonl on Render).';
comment on column public.bot_trade_log.bot_id is
  'Logical bot instance id (env POSITION_STATE_BOT_ID / TRADE_LOG_BOT_ID; default default).';
comment on column public.bot_trade_log."timestamp" is
  'ISO UTC timestamp string from the bot (mirrors JSONL timestamp field).';
comment on column public.bot_trade_log.source is
  'strategy (cron/main) or manual_test — used by --trade-log-report gate.';
comment on column public.bot_trade_log.order_id is
  'Alpaca order id when present; null for skips / non-actionable breadcrumbs.';

alter table public.bot_trade_log enable row level security;
