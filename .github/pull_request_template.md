## Summary

- What changed:
- Why this change is needed:

## Test Plan

- [ ] `pytest -q`
- [ ] `npm run test` (in `dashboard/`)
- [ ] `npm run lint` (in `dashboard/`)
- [ ] `npm run build` (in `dashboard/`)

## Security Checklist

- [ ] Inputs are validated at boundaries (API params/query/body, CLI args).
- [ ] Auth/authz logic is enforced for any mutating or sensitive route.
- [ ] Errors returned to clients are sanitized (no raw exception leakage).
- [ ] `request_id` is included in API error responses and route logs.
- [ ] No secrets/tokens/webhooks are logged or committed.
- [ ] New dependencies were reviewed for risk and necessity.
- [ ] Dependency scanning output was checked (Python + Node).
- [ ] Threats/abuse cases were considered for new behavior.

## Risk and Rollback

- Risk level (low/medium/high):
- Rollback approach:
