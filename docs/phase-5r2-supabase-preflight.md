# Phase 5R.2 — Supabase Preflight (Robinhood host schema)

**Status:** DRY RUN complete. Production DDL **not** applied.  
**Money moved:** $0.00 · **RH orders:** 0 · **Strategy:** 1.0.0 frozen · **Paper default:** intact · **Lives:** DISARMED  

Full readiness report: [`docs/phase-5r2-supabase-readiness-report.md`](./phase-5r2-supabase-readiness-report.md)

## Verdict

**B — APPLY_READY (pending operator approval)**

All five Robinhood host migrations are inventoried as **SAFE**, ordered, and missing from live project `algotrading` (`ggxihfikcxjlaxokjrrd`). Do not apply until Hemman explicitly approves.

| Grade | Meaning |
|-------|---------|
| **A** | APPLIED_AND_VERIFIED on live DB; security model confirmed |
| **B** | DB access OK; migrations SAFE + MISSING; apply plan ready; **no prod SQL run** |
| **C** | PARTIALLY_APPLIED / SCHEMA_DRIFT / REQUIRES_REVIEW |
| **D** | No secure DB access, DESTRUCTIVE deps, or blocked inventory |

## Baseline (main `86f16bc`)

| Check | Result |
|-------|--------|
| Phase 5R / PR #13 | Merged (`77424dc`) |
| Phase 5R.1 + RLS / PR #17 | Merged (`ab2d248`) |
| `STRATEGY_VERSION` | `1.0.0` |
| `EXECUTION_BROKER` in `render.yaml` | unset → Alpaca paper |
| Alpaca Live / RH agentic / handoff submit | all `False` / DISARMED |
| Supabase MCP | ready; project ACTIVE_HEALTHY |

## RH migration inventory (apply order)

| # | File | Objects | Safety |
|---|------|---------|--------|
| 1 | `20261007160000_bot_robinhood_execution_intents.sql` | intents table + indexes + RLS on | **SAFE** (`CREATE IF NOT EXISTS`) |
| 2 | `20261007160100_bot_robinhood_entry_reservations.sql` | daily entry UNIQUE PK + RLS on | **SAFE** |
| 3 | `20261007170000_bot_robinhood_equity_baselines.sql` | day/week/peak baselines + RLS on | **SAFE** |
| 4 | `20261007170100_bot_robinhood_rls_policies.sql` | service_role policies only; drop authenticated drafts | **SAFE** |
| 5 | `20261007220000_bot_robinhood_rls_drop_authenticated.sql` | idempotent drop of authenticated ALL | **SAFE** |

Stop on partial: if step N fails, do **not** continue. Do **not** blind re-run.

## Live DB classification (read-only MCP, 2026-10-07)

| Object | Classification |
|--------|----------------|
| `bot_robinhood_execution_intents` | **MISSING** |
| `bot_robinhood_entry_reservations` | **MISSING** |
| `bot_robinhood_equity_baselines` | **MISSING** |
| RH service_role policies | **MISSING** |
| `bot_position_state` / `bot_trade_log` | APPLIED (paper path; authenticated+service_role policies) |
| `bot_live_*` (Alpaca pilot) | APPLIED; RLS on; **no policies** (service_role bypasses; advisor INFO) |

No SCHEMA_DRIFT on RH objects (they do not exist yet). No authenticated `USING (true)` on RH tables in repo migrations (static tests).

## Connection / env names

| Env | Role |
|-----|------|
| `SUPABASE_URL` or `NEXT_PUBLIC_SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Backend only (Render / host). Never `NEXT_PUBLIC_*` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Dashboard public only — **not** for trading-control writes |

Project URL (non-secret): `https://ggxihfikcxjlaxokjrrd.supabase.co`

## Verify script

Read-only: [`scripts/sql/verify_robinhood_host_schema.sql`](../scripts/sql/verify_robinhood_host_schema.sql)

1. Supabase Dashboard → SQL Editor  
2. Paste file → Run  
3. Expect classification `MISSING` today; after approved apply → `APPLIED_AND_VERIFIED`

## Exact execution plan (approval required)

**Do not execute without Hemman approval.** Preferred: Supabase MCP `apply_migration` or SQL Editor, one file at a time, in order 1→5 above. After each step, re-run the verify script. Abort on any error or PARTIALLY_APPLIED.

Controlled application is **prepared only** in this phase.

## Operator checklist if MCP unavailable

1. Confirm org access to project `algotrading` / ref `ggxihfikcxjlaxokjrrd`  
2. Paste `SUPABASE_SERVICE_ROLE_KEY` only from Settings → API (secret), never anon  
3. Run verify SQL in SQL Editor  
4. Approve apply of the five SAFE migrations in order  

## Out of scope

No RH/Alpaca Live arming, no orders, no Render env changes, no Strategy 1.0.0 edits.
