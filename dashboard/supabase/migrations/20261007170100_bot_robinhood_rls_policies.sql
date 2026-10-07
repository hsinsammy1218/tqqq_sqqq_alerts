-- Explicit RLS policies for Robinhood host-mediated tables.
--
-- Python cron / host executor use SUPABASE_SERVICE_ROLE_KEY → JWT role
-- service_role (rolbypassrls=true). Policies document intent and keep writes
-- working if bypass were ever disabled. anon remains denied.

-- bot_robinhood_execution_intents --------------------------------------------

drop policy if exists bot_rh_exec_intents_service_role_all
  on public.bot_robinhood_execution_intents;
create policy bot_rh_exec_intents_service_role_all
  on public.bot_robinhood_execution_intents
  for all
  to service_role
  using (true)
  with check (true);

drop policy if exists bot_rh_exec_intents_authenticated_all
  on public.bot_robinhood_execution_intents;
create policy bot_rh_exec_intents_authenticated_all
  on public.bot_robinhood_execution_intents
  for all
  to authenticated
  using (true)
  with check (true);

-- bot_robinhood_entry_reservations -------------------------------------------

drop policy if exists bot_rh_entry_reservations_service_role_all
  on public.bot_robinhood_entry_reservations;
create policy bot_rh_entry_reservations_service_role_all
  on public.bot_robinhood_entry_reservations
  for all
  to service_role
  using (true)
  with check (true);

drop policy if exists bot_rh_entry_reservations_authenticated_all
  on public.bot_robinhood_entry_reservations;
create policy bot_rh_entry_reservations_authenticated_all
  on public.bot_robinhood_entry_reservations
  for all
  to authenticated
  using (true)
  with check (true);

-- bot_robinhood_equity_baselines ---------------------------------------------

drop policy if exists bot_rh_equity_baselines_service_role_all
  on public.bot_robinhood_equity_baselines;
create policy bot_rh_equity_baselines_service_role_all
  on public.bot_robinhood_equity_baselines
  for all
  to service_role
  using (true)
  with check (true);

drop policy if exists bot_rh_equity_baselines_authenticated_all
  on public.bot_robinhood_equity_baselines;
create policy bot_rh_equity_baselines_authenticated_all
  on public.bot_robinhood_equity_baselines
  for all
  to authenticated
  using (true)
  with check (true);

comment on policy bot_rh_exec_intents_service_role_all
  on public.bot_robinhood_execution_intents is
  'Render PENDING inserts + host claim/status via service_role. No OAuth tokens.';
comment on policy bot_rh_entry_reservations_service_role_all
  on public.bot_robinhood_entry_reservations is
  'Host BUY boundary reserves one entry/day via service_role.';
comment on policy bot_rh_equity_baselines_service_role_all
  on public.bot_robinhood_equity_baselines is
  'Host merges durable day/week/peak equity via service_role.';
