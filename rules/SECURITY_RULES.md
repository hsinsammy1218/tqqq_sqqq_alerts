# Security Rules

## Secret Management

- Secrets must be sourced from environment variables only.
- Never commit secrets to the repository.
- Rotate leaked or suspected compromised secrets immediately.
- Restrict service-role keys to server-only execution paths.

## Input Validation

- Validate all external inputs (CLI args, API params, provider payloads).
- Apply explicit allow-lists for symbol formats and enum-like values.
- Reject malformed inputs early with stable error codes.

## Authentication and Authorization

- All mutation endpoints require explicit auth.
- Prefer scoped service tokens for machine-to-machine route auth.
- Shared-secret auth is permitted only as backward-compatible fallback for internal cron integrations.
- Scope-to-route mapping must be explicit and least-privilege.
- If user-facing access is introduced, implement scoped authz (role/permission checks).

## Secure Logging

- Never log secrets, tokens, webhook URLs, or raw provider payloads containing sensitive data.
- Log only necessary troubleshooting context, with correlation IDs.
- Keep error details internal; client-facing responses must be sanitized.
- Include request IDs in API logs and error responses for auditability.

## Dependency Management

- Pin dependencies where practical and maintain lockfiles.
- Run vulnerability scans on a fixed cadence and on PRs.
- Upgrade critical vulnerabilities within defined SLA windows.

## Data Protection

- Collect/store only required fields for features and analytics.
- Minimize retention for transient ingest artifacts unless business justification exists.
- Document any tables containing sensitive operational metadata.

## Security Review Requirements

- Security-impacting PRs must include:
  - threat scenario
  - controls added/changed
  - validation tests.
- PRs must complete `.github/pull_request_template.md` security checklist items.
- Conduct periodic review of exposed routes and auth assumptions.
