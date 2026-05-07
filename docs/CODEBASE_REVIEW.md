# Codebase Engineering Review

Last reviewed: 2026-05-06  
Scope: `tqqq-sqqq-alerts` Python signal engine + Next.js dashboard + Supabase SQL migrations

## Executive Assessment

The codebase has improved materially since the initial audit. Core correctness and operational baselines were strengthened (scoring fix, regression tests, CI pipeline, API error sanitization, helper deduplication, and health endpoint). It is now substantially safer for ongoing development, but not yet a true "10/10 across all categories" because architectural decomposition and broader test coverage are still pending.

## Facts vs Assumptions

### Confirmed Facts

- The system is explicitly alert-only and does not place brokerage orders.
- The Python service computes technical scores and decisions, then emits console/CSV/Discord output.
- The dashboard ingests market/news data and technical snapshots into Supabase and serves read endpoints/pages.
- CI workflow now exists under `.github/workflows/ci.yml` and runs Python tests, dashboard lint/test/build, and dependency/security scans.
- A Python regression test suite exists (`tests/test_strategy_scoring.py`) and currently passes.
- API routes now use sanitized error responses (no raw `String(e)` returned to clients in updated routes).
- Route authorization and regime color mapping are centralized shared utilities.
- Dashboard now exposes `/api/health` with Supabase connectivity check.

### Assumptions (Require Confirmation)

- Dashboard APIs are intended for internal/trusted usage, not broad public exposure.
- Current workload is modest enough that sequential ingest loops remain within operational limits.

## Engineering Scorecard (1-10)

| Area | Score | Notes |
|---|---:|---|
| Architecture clarity | 7.5 | Responsibilities are clearer, but large core Python modules remain. |
| Code quality/maintainability | 7.5 | Duplication reduced and safety improved; major files still oversized. |
| Security posture | 8.8 | Error leakage fixed; scoped route-token auth and scan gates now implemented. |
| Performance/scalability | 6.5 | Stable for current scale; ingest flow still mostly sequential and route-heavy. |
| Testing discipline | 7 | Regression and API contract coverage expanded, with room for broader scenario depth. |
| DevOps/operability | 8.2 | CI now enforces tests/build/scans; observability baseline is in place. |
| Documentation quality | 9 | Comprehensive engineering docs and rules are now in place. |
| Production readiness (overall) | 7.5 | Strongly improved baseline; architecture and coverage expansion are next blockers. |

## System Purpose and Core Functionality

- Compute QQQ-derived technical conditions and emit TQQQ/SQQQ/CASH guidance.
- Persist state and journal outputs for continuity and operational traceability.
- Publish technical snapshots to Supabase for leaderboard-style consumption.
- Ingest and enrich market news context for dashboard analysis.

## Resolved Findings (Since Previous Review)

| Previous Risk | Status | Resolution |
|---|---|---|
| Bear volume scoring defect | Resolved | `strategy_scoring.py` now uses `volume < 20 avg` for bear condition. |
| Raw API exception leakage | Resolved | Standardized sanitized error responses introduced in updated routes. |
| Duplicate cron auth helper | Resolved | Replaced with scoped authorization utility in `dashboard/lib/api/routeAuth.ts`. |
| Duplicate regime CSS helper | Resolved | Centralized in `dashboard/lib/ui/regimeClass.ts`. |
| No CI pipeline | Resolved | Added GitHub Actions workflow for tests/lint/build. |
| No baseline tests | Partially resolved | Added regression tests; broader suite still needed. |

## Remaining Priority Risks

| Severity | Finding | Why It Matters | Recommended Action |
|---|---|---|---|
| High | Oversized research modules (`backtest.py`, `backtest_sweep.py`, `walk_forward.py`) | High change risk and difficult long-term ownership. | Split research runners into smaller domain/application modules. |
| Medium | Limited automated test breadth | Many edge-case strategy transitions and route branches remain untested. | Expand matrix/integration coverage for all route permutations and decision edges. |
| Medium | Ingest remains externally rate-bound | Provider fetch remains mostly serial under limits. | Introduce controlled concurrency or queued workers. |
| Medium | Security model still token-based without RBAC | Good for service automation, limited for multi-user operations. | Add identity-backed authn/authz if external operators are introduced. |
| Medium | Observability lacks centralized metrics sink | Request IDs and duration are present, but no persistent SLO backend. | Add metrics/tracing backend and alerting thresholds. |

## Technical Debt Summary (Current)

- **Structural debt:** Large Python modules and mixed responsibilities in route orchestration.
- **Quality debt:** Test suite exists but lacks breadth across key scenarios.
- **Performance debt:** Sequential ingest and route-level orchestration will constrain growth.
- **Operational debt:** Need deeper observability (request IDs, metrics, alerting).

## Production Readiness Review

### Current Strengths

- Clear product boundary and non-goals.
- Improved correctness and security baseline via targeted fixes.
- CI gate and regression tests now provide a real safety floor.
- Good setup/runbook documentation and defined engineering standards.

### Current Gaps to Reach "10 Across"

- Decompose god modules into maintainable service boundaries.
- Expand test coverage to critical-path breadth (strategy, API auth/validation, ingest behavior).
- Add richer observability and incident-oriented telemetry.
- Evolve ingest path for higher-volume scaling.

## Recommended Next Sequence (30-60 Days)

1. **Architecture:** split `main.py` and `strategy.py` into smaller testable modules.
2. **Testing:** add transition-matrix tests and API integration tests for auth/error contracts.
3. **Ingest hardening:** separate services from route glue and improve batch persistence strategy.
4. **Observability:** request IDs, structured route logs, and minimal latency/error metrics.

## Conclusion

This repository moved from "promising but fragile" to "operationally credible with guardrails." It is not yet all 10s, but it now has the foundation to achieve that with focused work on architecture decomposition, test breadth, and scalability hardening.
