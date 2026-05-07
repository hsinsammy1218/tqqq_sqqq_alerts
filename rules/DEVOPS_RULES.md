# DevOps Rules

## CI/CD Expectations

- Every PR must run automated checks:
  - lint
  - type-check
  - tests
  - build validation.
- CI must run dependency/security scans for Python and Node dependencies.
- Pull requests must run dependency review checks before merge.
- CI must fail on detected high/critical dependency vulnerabilities unless there is an explicit risk acceptance record.
- Enforced test entrypoints:
  - root: `pytest -q`
  - dashboard: `npm run test`
- Protect default branch with required status checks.
- Releases must be reproducible from tagged commits.

## Environment Management

- Keep `.env.example` files current and complete.
- Validate required env vars at startup and fail fast on missing critical keys.
- Separate local, staging, and production configurations explicitly.

## Deployment Strategy

- Use predictable deploy flow with rollback capability.
- Document deploy prerequisites and post-deploy checks.
- Avoid manual hotfixes without corresponding source changes.

## Monitoring Requirements

- Provide health checks for all runtime services.
- Track route latency, error rates, and job run outcomes.
- Define alert thresholds and on-call ownership for critical failures.
- Route handlers should emit request IDs and duration metadata.

## Logging Requirements

- Use structured logs with timestamps and correlation identifiers.
- Log start/end/result for scheduled jobs and ingest runs.
- Ensure logs are accessible for incident investigation.

## Infrastructure Standards

- Infrastructure/config changes must be versioned and reviewable.
- Scheduled jobs must be idempotent and observable.
- Database migrations must be forward-safe and documented.

## Operational Readiness Checklist (Pre-Production)

- CI gates active
- health endpoints available
- runbook documented
- rollback procedure tested
- critical alerts configured
- dependency scan baseline clean or accepted with risk record.
- no secrets detected in tracked files or release artifacts.

## Pre-Push Safety Checks

- Run local tests and lint before pushing.
- Run local dependency/security checks where practical (`pip-audit`, `npm audit`).
- Verify no secrets are staged:
  - inspect staged diff for credential-like values
  - ensure `.env` files with real values are not tracked.
