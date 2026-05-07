# Refactoring Roadmap

Last reviewed: 2026-05-06

## Goals

- Reduce regression risk in signal generation.
- Improve maintainability and team onboarding speed.
- Establish release confidence with automated quality gates.
- Prepare architecture for moderate scale growth.

## Prioritization Framework

Priority is based on: production risk, user impact, implementation effort, and dependency ordering.

## Phase 0: Immediate Guardrails (Week 1-2)

| Priority | Item | Outcome |
|---|---|---|
| P0 | Fix scoring symmetry bug and add regression tests | Prevent incorrect bullish/bearish weighting behavior. |
| P0 | Add CI for lint/type-check/build/tests | Enforce baseline quality on every PR. |
| P0 | Sanitize API 500 responses | Prevent leakage of internal error details. |
| P1 | Standardize API error envelope | Improve client behavior and troubleshooting consistency. |

Status: Completed.

## Phase 1: Quick Wins (Week 2-4)

1. Centralize duplicated helpers:
   - cron authorization helper for secured routes
   - shared UI regime style helper
2. Add route-level request validation for query params and symbols.
3. Pin Python top-level dependencies; establish monthly dependency update workflow.
4. Add `/api/health` endpoint with DB connectivity check.

Status: Completed.

## Phase 2: Medium-Term Improvements (Month 2)

### Python Service Modularization

- Split `main.py` into:
  - CLI parsing
  - run orchestrator
  - command handlers (`live`, `backtest`, `sweep`, `walk-forward`)
- Split `strategy.py` into:
  - decision policy (pure domain)
  - position transitions
  - output formatting
- Add domain-level unit tests for transition matrix and threshold logic.

Status: In progress (module split completed; expand transition matrix coverage further).

### Dashboard Service Layer

- Move ingest flow into services:
  - `provider_client`
  - `normalizer/enricher`
  - `dedupe_persistence`
  - `ingest_audit`
- Keep route handlers thin: auth, validation, service call, response mapping.

Status: In progress (ingest service extracted; continue service extraction for remaining routes).

## Phase 3: Long-Term Evolution (Month 3+)

1. Introduce API versioning (`/api/v1/...`) and stable response contracts.
2. Improve ingestion scalability:
   - DB-side upsert/conflict strategy
   - optional queue-based processing for provider calls.
3. Add observability stack:
   - structured request logs with request IDs (completed baseline)
   - endpoint latency/error metrics (completed baseline headers/log fields)
   - alerting thresholds (pending)
4. Formalize ADRs and architecture governance in docs.

## Suggested Implementation Order

1. Correctness-critical fixes (scoring + API error leakage)
2. CI + tests baseline
3. Helper deduplication and validation consistency
4. Python/domain module decomposition
5. Dashboard ingest service decomposition
6. Scalability/observability enhancements

## Risk Management During Refactor

- Use characterization tests before moving logic.
- Prefer incremental PRs (small surface area, reversible).
- Keep behavior parity checks (golden test inputs/outputs).
- Add migration notes for any API response changes.

## Success Metrics

| Metric | Baseline | Target |
|---|---:|---:|
| Decision logic unit test coverage | low/none | >= 80% critical paths |
| CI pass rate on PRs | N/A | >= 95% |
| Mean time to diagnose route failures | high/manual | reduced with structured logs |
| Average file size in core domain modules | high | reduced by 30-50% |
| Escaped internal errors to API clients | present | 0 |
