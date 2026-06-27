# Engineering Change Log

Purpose: Track meaningful engineering, architecture, security, and process updates in this repository.

## 2026-05-06

### Architecture and Code Quality

- Split strategy into smaller modules:
  - `strategy_types.py`
  - `strategy_position.py`
  - `strategy_decision.py`
  - `strategy_formatting.py`
  - kept `strategy.py` as backward-compatible facade.
- Extracted runtime concerns from `main.py`:
  - `cli_args.py`
  - `runtime_logging.py`
  - `health_check.py`
- Fixed scoring correctness bug in `strategy_scoring.py` (bear volume condition now uses `<`).

### API Hardening and Observability

- Standardized sanitized API error responses with structured error codes and `request_id`.
- Added request observability utilities and metadata:
  - `x-request-id`
  - `x-duration-ms`
  - `x-status`
- Added dashboard health route:
  - `dashboard/app/api/health/route.ts`
- Extracted ingest logic into service:
  - `dashboard/lib/news/ingestService.ts`
- Improved ingest persistence with conflict-aware upsert strategy.

### Security Enhancements

- Introduced scoped route authorization:
  - `dashboard/lib/api/routeAuth.ts`
  - scopes: `news:ingest`, `technical:revalidate`
  - env: `ROUTE_AUTH_TOKENS_JSON`
  - backward-compatible fallback to `CRON_SECRET`.
- Added PR security checklist template:
  - `.github/pull_request_template.md`

### CI/CD and Testing

- Added/expanded CI workflow:
  - Python: tests + `pip-audit`
  - Dashboard: lint + test + build + `npm audit --audit-level=high`
  - PR dependency review action.
- Added/expanded tests:
  - `tests/test_strategy_scoring.py`
  - `tests/test_decide_transition_matrix.py`
  - `dashboard/tests/api_contracts.test.ts`

### Documentation and Standards

- Created and iteratively updated production-grade docs under `docs/` (later moved to `archive/docs/`).
- Created and updated engineering standards under `rules/`.
- Synced docs/rules to reflect implemented changes in architecture, security, testing, and DevOps.
- Added explicit no-secrets-in-git governance and pre-push secret-safety requirements in security/devops rules.

## Update Guidance

- Add entries for behavior-changing or process-changing work.
- Keep each entry concise and factual.
- Include date, category, and impacted file/module paths where relevant.
