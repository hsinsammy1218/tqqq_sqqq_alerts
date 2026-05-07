# Security Review

Last reviewed: 2026-05-06

## Scope

- Python signal engine
- Next.js dashboard routes/pages
- Supabase access patterns and migrations
- Environment and secret handling in repository conventions

## Findings Summary

| Severity | Count |
|---|---:|
| High | 0 |
| Medium | 5 |
| Low | 3 |

## Findings Closed in This Cycle

- **SEC-001 closed:** Mutation routes now support scoped tokens via `ROUTE_AUTH_TOKENS_JSON` with per-route scopes (`news:ingest`, `technical:revalidate`) and backward-compatible cron-secret support.
- **SEC-002 closed:** CI now enforces dependency/security scanning (`pip-audit`, `npm audit --audit-level=high`, dependency review action on PRs).
- **SEC-003 partially closed:** API auth/error contract integration tests were expanded to cover unauthorized, wrong-scope, invalid input, and parameter normalization cases; additional branch coverage is still recommended.

## Medium Severity Findings

### SEC-004: Token auth model is service-scoped, not user-scoped

- Scoped service tokens now exist and materially improve route-level authorization.
- Remaining gap: no user identity/RBAC model for interactive operator workflows.

### SEC-005: Service-role key access surface should stay tightly constrained

- Service-role client is server-side, but boundaries should be explicit and limited to infrastructure adapters.

### SEC-006: Input validation strategy is improved but not yet schema-standardized

- Several routes now enforce stronger validation and parameter clamping, with tests.
- Remaining improvement: adopt schema-based validation (`zod` or equivalent) consistently.

### SEC-007: Environment schema enforcement inconsistent across subsystems

- Python has structured settings loading; dashboard env validation should be similarly strict and fail-fast at startup.

## Low Severity Findings

- Python dependency pinning is broad and may increase patch drift risk.
- No formal security review checklist is documented for PRs.
- CSP/security header hardening in web app is not centrally documented.

## Positive Controls Observed

- Secret files are not committed by default patterns.
- Critical mutation routes include scoped token checks (with cron-secret compatibility).
- API responses now include structured errors with request IDs.
- Request IDs and duration metadata are emitted for key routes.
- No obvious unsafe HTML rendering pattern observed in dashboard rendering paths.

## Mitigation Plan

## Immediate (1-2 weeks)

1. Keep CI security scans enforced and tune severity thresholds based on policy.
2. Expand API auth/validation tests to cover all route handlers.
3. Keep PR security checklist mandatory in code review.

## Near-term (1-2 months)

1. Introduce standardized request validation and error envelope.
2. Add request IDs and structured route logging.
3. Formalize security checklist in PR template/process docs.

## Strategic (Quarter)

1. Evaluate auth model for multi-operator roles.
2. Define security headers baseline and runtime hardening.
3. Add recurring dependency/security scanning with owner notifications.

## Secure Coding Concerns to Track

- Never return raw exception payloads externally.
- Keep service-role operations in server-only modules.
- Validate and constrain all request inputs before persistence/query.
- Avoid logging secrets and rotate webhook/tokens periodically.
