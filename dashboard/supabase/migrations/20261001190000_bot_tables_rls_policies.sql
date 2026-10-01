-- Explicit RLS policies for alert-bot tables.
--
-- Design: Python cron uses SUPABASE_SERVICE_ROLE_KEY → JWT role service_role.
-- service_role already has rolbypassrls=true, so it succeeds even with zero
-- policies. These policies document intent and keep inserts working if
-- bypass were ever disabled. authenticated is allowed for dashboard/server
-- paths that use a user JWT. anon remains denied (no policies → RLS deny).
--
-- If Render cron still gets 42501 after this migration, SUPABASE_SERVICE_ROLE_KEY
-- is almost certainly the anon public key — paste service_role (secret) instead.

-- bot_position_state ----------------------------------------------------------

drop policy if exists bot_position_state_service_role_all on public.bot_position_state;
create policy bot_position_state_service_role_all
  on public.bot_position_state
  for all
  to service_role
  using (true)
  with check (true);

drop policy if exists bot_position_state_authenticated_all on public.bot_position_state;
create policy bot_position_state_authenticated_all
  on public.bot_position_state
  for all
  to authenticated
  using (true)
  with check (true);

-- bot_trade_log ---------------------------------------------------------------

drop policy if exists bot_trade_log_service_role_all on public.bot_trade_log;
create policy bot_trade_log_service_role_all
  on public.bot_trade_log
  for all
  to service_role
  using (true)
  with check (true);

drop policy if exists bot_trade_log_authenticated_all on public.bot_trade_log;
create policy bot_trade_log_authenticated_all
  on public.bot_trade_log
  for all
  to authenticated
  using (true)
  with check (true);

comment on policy bot_position_state_service_role_all on public.bot_position_state is
  'Bot / server writes via service_role JWT (also bypasses RLS).';
comment on policy bot_position_state_authenticated_all on public.bot_position_state is
  'Authenticated dashboard/server paths may read/write position state.';
comment on policy bot_trade_log_service_role_all on public.bot_trade_log is
  'Bot / server appends via service_role JWT (also bypasses RLS).';
comment on policy bot_trade_log_authenticated_all on public.bot_trade_log is
  'Authenticated dashboard/server paths may read/write trade log.';
