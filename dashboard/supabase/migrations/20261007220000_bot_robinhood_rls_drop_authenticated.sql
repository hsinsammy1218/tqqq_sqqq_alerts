-- Corrective: drop any authenticated ALL policies on RH trading-control tables.
-- Idempotent. Covers environments that applied an earlier draft of
-- 20261007170100 that granted TO authenticated … USING (true) WITH CHECK (true).
-- Desired model: anon NO · authenticated NO · service_role AUTHORITY.

drop policy if exists bot_rh_exec_intents_authenticated_all
  on public.bot_robinhood_execution_intents;
drop policy if exists bot_rh_entry_reservations_authenticated_all
  on public.bot_robinhood_entry_reservations;
drop policy if exists bot_rh_equity_baselines_authenticated_all
  on public.bot_robinhood_equity_baselines;
