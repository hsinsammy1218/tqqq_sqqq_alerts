-- Alert-bot position memory for cloud runners (Render Cron). Service role only.

create table if not exists public.bot_position_state (
  bot_id text primary key,
  symbol text,
  entry_price double precision,
  entry_time text,
  last_signal text,
  updated_at text,
  constraint bot_position_state_symbol_chk
    check (symbol is null or symbol in ('TQQQ', 'SQQQ'))
);

comment on table public.bot_position_state is
  'Persistent TQQQ/SQQQ/flat memory for the Python alert bot (file alternative for ephemeral hosts).';
comment on column public.bot_position_state.bot_id is
  'Logical bot instance id (env POSITION_STATE_BOT_ID; default default).';
comment on column public.bot_position_state.symbol is
  'TQQQ, SQQQ, or null when flat — mirrors position_state.json.';
comment on column public.bot_position_state.entry_time is
  'ISO timestamp string from the bot (not necessarily timestamptz).';
comment on column public.bot_position_state.updated_at is
  'ISO timestamp string when the bot last saved this row.';

alter table public.bot_position_state enable row level security;
