# Phase 5R.2 — Supabase Readiness Report

**Repo:** `hsinsammy1218/tqqq_sqqq_alerts`  
**Main tip audited:** `86f16bcd8871781451f91490cd4f5fd87ec62923`  
**Branch (artifacts):** `cursor/phase-5r2-supabase-842a` @ `62dfe0a72cf7144492fca0fe9893fb83206779f2`  
**Money:** $0.00 · **Orders:** 0 · **DRY RUN:** yes (no production DDL applied)

## Verdict: **B — APPLY_READY**

DB access was available (Supabase MCP read-only on project `algotrading` / `ggxihfikcxjlaxokjrrd`). Robinhood host tables and RLS are **MISSING**. All five repo migrations are **SAFE** and ordered. Apply only after explicit Hemman approval.

| Grade | Definition | This run |
|-------|------------|----------|
| A | APPLIED_AND_VERIFIED + security OK | No |
| **B** | Access OK; SAFE plan; objects MISSING; no prod apply | **Yes** |
| C | PARTIAL / DRIFT / REQUIRES_REVIEW | No |
| D | No access / DESTRUCTIVE / blocked | No |

## 1. Baseline

| Item | Result |
|------|--------|
| Phase 5R (#13) | Merged — `77424dc` |
| Phase 5R.1 + RLS (#17) | Merged — `ab2d248` |
| Tip after Dependabot | `86f16bc` |
| `STRATEGY_VERSION` | `1.0.0` |
| Paper default | `EXECUTION_BROKER` unset in `render.yaml` → paper-api |
| `ALPACA_LIVE_SUBMISSION_IMPLEMENTED` | `False` |
| RH `LIVE_SUBMISSION_IMPLEMENTED` | `False` |
| `HANDOFF_SUBMISSION_IMPLEMENTED` | `False` |
| Render RH arming | Not set (comments forbid) |

## 2. Migration inventory

Path: `dashboard/supabase/migrations/`

### Robinhood host chain (required for 5R.2)

| Order | Migration | Purpose | Dependency | Safety |
|-------|-----------|---------|------------|--------|
| 1 | `20261007160000_bot_robinhood_execution_intents.sql` | PENDING intents + unique `client_order_id` + claim columns | none | **SAFE** |
| 2 | `20261007160100_bot_robinhood_entry_reservations.sql` | Daily entry UNIQUE `(bot_id, trading_day, entry_slot)` | none (logical after intents) | **SAFE** |
| 3 | `20261007170000_bot_robinhood_equity_baselines.sql` | Durable day/week/peak equity | none | **SAFE** |
| 4 | `20261007170100_bot_robinhood_rls_policies.sql` | service_role AUTHORITY; drop authenticated drafts | tables 1–3 | **SAFE** |
| 5 | `20261007220000_bot_robinhood_rls_drop_authenticated.sql` | Idempotent corrective drop | policies may or may not exist | **SAFE** |

### Already applied (non-RH; context)

Remote migration history includes: `bot_position_state`, `bot_trade_log`, `bot_tables_rls_policies`, `bot_live_order_claims`, `bot_live_circuit_state`, `bot_live_entry_reservations`.

Repo also has shadow/dashboard migrations not required for RH host pilot and not re-applied here.

## 3. Connection audit

| Signal | Result |
|--------|--------|
| Supabase MCP `namespaceStatus` | `ready` |
| Projects visible | `algotrading` ACTIVE_HEALTHY; others INACTIVE (ignored) |
| Project id / ref | `ggxihfikcxjlaxokjrrd` |
| Host | `db.ggxihfikcxjlaxokjrrd.supabase.co` (no credentials printed) |
| Env names in repo | `SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` |
| Secret exposure | No `NEXT_PUBLIC_SUPABASE_SERVICE_ROLE*` (existing RLS tests) |

## 4. Schema compare (read-only)

| Expected | Live | Classification |
|----------|------|----------------|
| `bot_robinhood_execution_intents` | absent | **MISSING** |
| `bot_robinhood_entry_reservations` | absent | **MISSING** |
| `bot_robinhood_equity_baselines` | absent | **MISSING** |
| RH RLS policies | absent | **MISSING** |
| `bot_live_*` | present, RLS on, 0 policies | APPLIED (Alpaca path; advisor INFO) |
| `bot_position_state` / `bot_trade_log` | present + policies | APPLIED |

`to_regclass` for all three RH tables returned `null`. No PARTIALLY_APPLIED / SCHEMA_DRIFT on RH objects.

## 5. Security / RLS validation

### Target model (Phase 5R.1 amendment)

| Role | RH trading-control tables |
|------|---------------------------|
| `anon` | **DENIED** |
| `authenticated` | **DENIED** |
| `service_role` | **AUTHORIZED** (`rolbypassrls=true` + explicit policies) |

### Live role flags

| Role | `rolbypassrls` |
|------|----------------|
| anon | false |
| authenticated | false |
| service_role | true |

### Repo static

- `tests/test_robinhood_host_rls.py` — no `CREATE POLICY … TO authenticated` on RH tables; corrective drop present.
- Migrations enable RLS on all three RH tables before policies.

### Advisors (security)

INFO: `bot_live_order_claims`, `bot_live_circuit_state`, `bot_live_entry_reservations` have RLS enabled with no policies (deny for non-bypass roles). Out of RH apply scope; note only.

### Flag watch

No `USING (true)` authenticated policies on RH tables in migrations. After apply, re-run verify script section 3 — any authenticated policy → treat as **C / SCHEMA_DRIFT** and stop.

## 6. Dependency + safety analysis

| Migration | Class | Notes |
|-----------|-------|-------|
| intents / reservations / equity | **SAFE** | `CREATE TABLE IF NOT EXISTS`; enable RLS; no DROP TABLE |
| RLS policies | **SAFE** | `DROP POLICY IF EXISTS` + create service_role only |
| drop authenticated | **SAFE** | Drop only; empty no-op if never created |

No **DESTRUCTIVE** statements in the RH chain. No **REQUIRES_REVIEW** DDL for empty-table create path.

## 7. Verify script

Added: `scripts/sql/verify_robinhood_host_schema.sql` (SELECT/WITH only).

**SQL Editor instructions:** Dashboard → SQL Editor → paste → Run. Do not paste migration files into the same session without approval. After apply, expect `APPLIED_AND_VERIFIED` for all three tables.

Deterministic tests: `tests/test_robinhood_host_schema_verify.py`.

## 8. Exact execution plan (DO NOT RUN without approval)

1. Confirm kill switches remain false; no Render `EXECUTION_BROKER=robinhood_host_handoff`.
2. Snapshot: run `scripts/sql/verify_robinhood_host_schema.sql` → expect MISSING.
3. Apply **one** migration (MCP `apply_migration` or SQL Editor), order 1→5.
4. Re-run verify after each step. On error or PARTIALLY_APPLIED → **STOP**.
5. After step 5: all three tables `APPLIED_AND_VERIFIED`; policies service_role only.
6. Optional: `get_advisors` security; confirm no authenticated RH policies.
7. Record applied versions; do not arm RH.

Blind re-run of already-applied `CREATE POLICY` without `DROP IF EXISTS` is unsafe — use repo files as written (they include drops).

## 9. Controlled application status

**Prepared only.** This phase did **not** call `apply_migration` for RH objects and did not execute production DDL.

## 10. Tests

| Check | Result |
|-------|--------|
| `pytest -q` | **395 passed** (was 391 + 4 verify-script tests) |
| RH RLS + verify | 11 passed |
| Branch tip | `5ab9d8a9ad2382d236e1997de1b8987fa3141a84` |
| `python3 -m compileall -q .` | ok |
| `pip check` | No broken requirements |

## 11. Docs

| Path | Role |
|------|------|
| `docs/phase-5r2-supabase-preflight.md` | Operator preflight |
| `docs/phase-5r2-supabase-readiness-report.md` | This report |
| Store mirrors under `/cursor/stores/self/docs/` | Project handoff |

## 12. Next operator action

Hemman: approve apply of the five SAFE migrations in order, then re-run verify SQL. Until then, verdict remains **B**. Host-mediated RH preflight still requires authenticated MCP host separately (Phase 5R.1 A path) after schema is A.
