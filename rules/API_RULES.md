# API Rules

Applies to all routes under `dashboard/app/api`.

## API Structure

- Group endpoints by domain (`news`, `technical`) with predictable naming.
- Introduce versioned namespace for stable public contracts (`/api/v1/...`) when breaking changes are possible.
- Keep route handlers lightweight and delegate to service modules.

## Request Validation Requirements

- Validate all route params and query values at route boundaries.
- Reject invalid inputs with `400` and standardized error payload.
- Clamp pagination and window parameters to safe limits.

## Response Standards

- Successful responses must be JSON with stable field names and types.
- Use typed response builders to enforce consistent contracts.
- Include pagination metadata where collections are returned.
- Include `request_id` on success responses for traceability.

## Error Response Standards

Use this shape for all failures:

```json
{
  "error": {
    "code": "STRING_CODE",
    "message": "Human-readable message",
    "request_id": "optional-correlation-id"
  }
}
```

- Never return raw exceptions to clients.
- Map internal failures to stable error codes.
- Include `request_id` in error payloads.

## Authentication and Authorization

- Mutation endpoints require authentication and authorization checks.
- Shared-secret cron auth is allowed only for machine-to-machine internal routes.
- Secrets must be loaded server-side only and never exposed to client bundles.

## Idempotency and Safety

- Ingest/revalidate endpoints should be idempotent or safely retryable.
- Prefer upsert/conflict-aware persistence over pre-read dedupe where feasible.

## API Documentation

- Every endpoint must document:
  - purpose
  - required auth
  - parameters and constraints
  - response schema
  - error codes.
  - observability headers (`x-request-id`, `x-duration-ms`, `x-status` where applicable).
