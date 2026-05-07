# Testing Rules

## Coverage Expectations

- Critical decision logic paths require high-confidence coverage (target >= 80% on core strategy modules).
- Route auth, validation, and error behavior must have integration coverage.
- New features must include tests or a documented exception.

## Unit Testing Rules

- Unit test pure business logic first (scoring, regime classification, state transitions).
- Use deterministic fixtures for indicator snapshots and expected decisions.
- Avoid network and database calls in unit tests.

## Integration Testing Standards

- Validate API contracts, auth enforcement, and error envelopes.
- Test ingest and publish persistence paths against controlled test data.
- Include negative tests (invalid symbols, missing auth, malformed inputs).
- Maintain API contract tests for structured errors and request IDs.
- Include scoped-auth matrix tests (valid scope, invalid scope, missing token, fallback token).

## End-to-End Strategy

- Add minimal E2E smoke tests for key user journeys:
  - leaderboard renders latest snapshots
  - ticker page loads technical + news context
  - market news pagination/filtering works.

## Mocking Guidelines

- Mock external providers (KlickAnalytics/Finnhub) at boundaries.
- Prefer contract-based mocks over ad hoc object shapes.
- Avoid over-mocking internal domain logic under test.

## Test Naming Conventions

- Use clear behavior-oriented names:
  - `should_return_cash_when_confidence_below_threshold`
  - `returns_401_when_cron_secret_missing`
- Test names should communicate scenario + expected outcome.

## Regression Protection

- Any bug fix must include a regression test reproducing prior faulty behavior.
- Strategy rule changes require before/after fixture snapshots and documented expected impact.

## CI Enforcement

- Tests run on every PR.
- Merges blocked on failing tests, lint, type-check, or build.
- Python command baseline: `pytest -q`.
- Dashboard command baseline: `npm run test` (Vitest).
