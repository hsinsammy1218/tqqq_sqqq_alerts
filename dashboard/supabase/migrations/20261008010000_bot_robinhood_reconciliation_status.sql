-- Phase 5R.4.1: allow RECONCILIATION_REQUIRED on host execution intents.
-- Used when place outcome is ambiguous — no auto resubmit; operator/order-history recon.

alter table public.bot_robinhood_execution_intents
  drop constraint if exists bot_rh_exec_intents_status;

alter table public.bot_robinhood_execution_intents
  add constraint bot_rh_exec_intents_status check (
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
      'RECONCILIATION_REQUIRED',
      'BLOCKED',
      'EXPIRED_INTENT',
      'SUPERSEDED'
    )
  );

comment on constraint bot_rh_exec_intents_status
  on public.bot_robinhood_execution_intents is
  'Includes RECONCILIATION_REQUIRED for ambiguous post-submit outcomes (no auto resubmit).';
