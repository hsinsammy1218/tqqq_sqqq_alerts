-- Explicit RLS policies for Robinhood host-mediated trading-control tables.
--
-- Access model (Phase 5R.1 security amendment):
--   anon           → NO ACCESS (RLS enabled, zero policies for anon)
--   authenticated  → NO ACCESS (no dashboard/browser mutation of trading control)
--   service_role   → AUTHORITY (Render handoff + authenticated MCP host backend)
--
-- Python cron / host executor use SUPABASE_SERVICE_ROLE_KEY → JWT role
-- service_role (rolbypassrls=true). Policies document intent and keep writes
-- working if bypass were ever disabled. Do NOT grant authenticated true/true.
-- No OAuth tokens are stored in these tables. Public anon key must never be
-- used for trading writes.

-- bot_robinhood_execution_intents --------------------------------------------

drop policy if exists bot_rh_exec_intents_authenticated_all
  on public.bot_robinhood_execution_intents;
drop policy if exists bot_rh_exec_intents_service_role_all
  on public.bot_robinhood_execution_intents;
create policy bot_rh_exec_intents_service_role_all
  on public.bot_robinhood_execution_intents
  for all
  to service_role
  using (true)
  with check (true);

-- bot_robinhood_entry_reservations -------------------------------------------

drop policy if exists bot_rh_entry_reservations_authenticated_all
  on public.bot_robinhood_entry_reservations;
drop policy if exists bot_rh_entry_reservations_service_role_all
  on public.bot_robinhood_entry_reservations;
create policy bot_rh_entry_reservations_service_role_all
  on public.bot_robinhood_entry_reservations
  for all
  to service_role
  using (true)
  with check (true);

-- bot_robinhood_equity_baselines ---------------------------------------------

drop policy if exists bot_rh_equity_baselines_authenticated_all
  on public.bot_robinhood_equity_baselines;
drop policy if exists bot_rh_equity_baselines_service_role_all
  on public.bot_robinhood_equity_baselines;
create policy bot_rh_equity_baselines_service_role_all
  on public.bot_robinhood_equity_baselines
  for all
  to service_role
  using (true)
  with check (true);

comment on policy bot_rh_exec_intents_service_role_all
  on public.bot_robinhood_execution_intents is
  'service_role only. anon/authenticated have no policies (deny). No OAuth tokens.';
comment on policy bot_rh_entry_reservations_service_role_all
  on public.bot_robinhood_entry_reservations is
  'service_role only at host BUY boundary. anon/authenticated deny.';
comment on policy bot_rh_equity_baselines_service_role_all
  on public.bot_robinhood_equity_baselines is
  'service_role only for durable equity baselines. anon/authenticated deny.';

comment on table public.bot_robinhood_execution_intents is
  'Host-mediated Robinhood TradeIntents. service_role backend only. No OAuth tokens. anon/authenticated: no access.';
comment on table public.bot_robinhood_entry_reservations is
  'One RH host-pilot entry per bot/day/slot. service_role backend only. anon/authenticated: no access.';
comment on table public.bot_robinhood_equity_baselines is
  'Durable RH host equity baselines. service_role backend only. anon/authenticated: no access.';
