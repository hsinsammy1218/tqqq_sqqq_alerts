-- Phase 5R.2 — Read-only verification of Robinhood host-mediated schema.
--
-- HOW TO RUN (Supabase Dashboard → SQL Editor, role: postgres / service):
--   1. Open project Settings → confirm you are on the algotrading project.
--   2. SQL Editor → New query → paste this entire file → Run.
--   3. Review each result set. Do NOT run migration DDL from this file.
--
-- Expected access model after migrations are applied:
--   anon           → DENIED (RLS on, zero anon policies)
--   authenticated  → DENIED (no authenticated policies on bot_robinhood_*)
--   service_role   → AUTHORIZED (explicit FOR ALL policies + rolbypassrls)
--
-- Classification helper (operator):
--   APPLIED_AND_VERIFIED — table exists, RLS on, service_role policy present,
--                          no authenticated USING(true) policy
--   MISSING              — to_regclass is null
--   PARTIALLY_APPLIED    — table exists but RLS off or service_role policy missing
--   SCHEMA_DRIFT         — unexpected columns / constraints (manual review)
--   UNKNOWN              — cannot query (no access)

-- ---------------------------------------------------------------------------
-- 1) Presence of RH trading-control tables
-- ---------------------------------------------------------------------------
select
  t.expected_table,
  to_regclass(format('public.%I', t.expected_table)) is not null as exists,
  case
    when to_regclass(format('public.%I', t.expected_table)) is null then 'MISSING'
    else 'PRESENT'
  end as presence
from (
  values
    ('bot_robinhood_execution_intents'),
    ('bot_robinhood_entry_reservations'),
    ('bot_robinhood_equity_baselines')
) as t(expected_table)
order by t.expected_table;

-- ---------------------------------------------------------------------------
-- 2) RLS flags
-- ---------------------------------------------------------------------------
select
  c.relname as table_name,
  c.relrowsecurity as rls_enabled,
  c.relforcerowsecurity as rls_forced
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relkind = 'r'
  and c.relname in (
    'bot_robinhood_execution_intents',
    'bot_robinhood_entry_reservations',
    'bot_robinhood_equity_baselines'
  )
order by c.relname;

-- ---------------------------------------------------------------------------
-- 3) Policies on RH tables (expect service_role only; flag authenticated)
-- ---------------------------------------------------------------------------
select
  tablename,
  policyname,
  roles::text as roles,
  cmd,
  qual as using_expr,
  with_check as with_check_expr,
  case
    when 'authenticated' = any (roles) and coalesce(qual, '') = 'true'
      then 'FLAG: authenticated USING(true)'
    when 'anon' = any (roles)
      then 'FLAG: anon policy present'
    when 'service_role' = any (roles)
      then 'OK: service_role'
    else 'REVIEW'
  end as assessment
from pg_policies
where schemaname = 'public'
  and tablename in (
    'bot_robinhood_execution_intents',
    'bot_robinhood_entry_reservations',
    'bot_robinhood_equity_baselines'
  )
order by tablename, policyname;

-- ---------------------------------------------------------------------------
-- 4) Role bypass flags (service_role must bypass RLS)
-- ---------------------------------------------------------------------------
select rolname, rolbypassrls, rolsuper
from pg_roles
where rolname in ('anon', 'authenticated', 'service_role')
order by rolname;

-- ---------------------------------------------------------------------------
-- 5) Key constraints / indexes for claim + reservation authority
-- ---------------------------------------------------------------------------
select
  tc.table_name,
  tc.constraint_type,
  tc.constraint_name
from information_schema.table_constraints tc
where tc.table_schema = 'public'
  and tc.table_name in (
    'bot_robinhood_execution_intents',
    'bot_robinhood_entry_reservations',
    'bot_robinhood_equity_baselines'
  )
order by tc.table_name, tc.constraint_type, tc.constraint_name;

select
  indexname,
  tablename,
  indexdef
from pg_indexes
where schemaname = 'public'
  and tablename in (
    'bot_robinhood_execution_intents',
    'bot_robinhood_entry_reservations',
    'bot_robinhood_equity_baselines'
  )
order by tablename, indexname;

-- ---------------------------------------------------------------------------
-- 6) Column inventory (for SCHEMA_DRIFT spot-checks)
-- ---------------------------------------------------------------------------
select
  table_name,
  column_name,
  data_type,
  is_nullable,
  column_default
from information_schema.columns
where table_schema = 'public'
  and table_name in (
    'bot_robinhood_execution_intents',
    'bot_robinhood_entry_reservations',
    'bot_robinhood_equity_baselines'
  )
order by table_name, ordinal_position;

-- ---------------------------------------------------------------------------
-- 7) Summary classification (one row per expected table)
-- ---------------------------------------------------------------------------
with expected as (
  select * from (
    values
      ('bot_robinhood_execution_intents'),
      ('bot_robinhood_entry_reservations'),
      ('bot_robinhood_equity_baselines')
  ) as v(table_name)
),
rls as (
  select c.relname as table_name, c.relrowsecurity as rls_enabled
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r'
),
svc as (
  select distinct tablename as table_name
  from pg_policies
  where schemaname = 'public'
    and 'service_role' = any (roles)
),
auth_true as (
  select distinct tablename as table_name
  from pg_policies
  where schemaname = 'public'
    and 'authenticated' = any (roles)
    and coalesce(qual, '') = 'true'
)
select
  e.table_name,
  case
    when to_regclass(format('public.%I', e.table_name)) is null then 'MISSING'
    when coalesce(r.rls_enabled, false) is not true then 'PARTIALLY_APPLIED'
    when a.table_name is not null then 'SCHEMA_DRIFT'
    when s.table_name is null then 'PARTIALLY_APPLIED'
    else 'APPLIED_AND_VERIFIED'
  end as classification
from expected e
left join rls r on r.table_name = e.table_name
left join svc s on s.table_name = e.table_name
left join auth_true a on a.table_name = e.table_name
order by e.table_name;

-- End of read-only verify script. No DDL. No DML.
