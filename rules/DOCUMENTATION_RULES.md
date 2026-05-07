# Documentation Rules

## README Expectations

- Root README must explain purpose, non-goals, setup, run modes, and operational caveats.
- Subsystem READMEs must cover local setup, environment variables, and common workflows.
- Keep examples executable and current.

## File and Module Documentation

- Complex modules require top-level docstring/overview section describing responsibilities and boundaries.
- Public interfaces should include concise parameter/return behavior notes.
- Document invariants and assumptions for strategy logic.

## API Documentation

- Every route must document:
  - method and path
  - auth requirements
  - request parameters
  - response shape
  - error codes.
  - observability behavior (request ID and latency headers when present).

## Architecture Documentation

- Maintain architecture overview with:
  - component boundaries
  - data flow diagrams
  - dependency direction rules.
- Significant design choices require ADR entries.

## Commenting Expectations

- Favor intent-focused comments over line-by-line narration.
- Add comments for non-obvious constraints, fallback behavior, and trade-offs.
- Remove stale comments during code changes.

## Onboarding Standards

- Include fast-start path for new contributors.
- Provide troubleshooting section for common setup and runtime failures.
- Reference runbooks for scheduled jobs, incident handling, and rollback.

## Documentation Maintenance

- Documentation updates are required in PRs that change behavior or interfaces.
- At least quarterly, review docs for drift against implementation.
- Use explicit "fact vs assumption" sections in audit-style docs.
