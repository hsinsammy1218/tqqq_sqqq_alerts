-- Allow connected-shadow audit rows. Status stays NOT_SUBMITTED.
-- Not applied from the bot process. Run in Supabase only when you want copies.

alter table public.bot_shadow_orders
  drop constraint if exists bot_shadow_orders_shadow_mode;

alter table public.bot_shadow_orders
  add constraint bot_shadow_orders_shadow_mode
  check (execution_mode in ('shadow', 'connected_shadow'));

comment on table public.bot_shadow_orders is
  'Robinhood shadow and connected-shadow intents. Rows are would-be orders, never live fills.';
