# Coding Standards

Applies to Python and TypeScript code in this repository.

## Naming Conventions

- Use descriptive domain names (`indicator_snapshot`, `decision_result`, `ingest_run`).
- Python: `snake_case` for functions/variables, `PascalCase` for classes.
- TypeScript/React: `camelCase` for variables/functions, `PascalCase` for components/types.
- File names should reflect primary responsibility; avoid generic names like `utils2`.

## File Structure Standards

- One primary responsibility per file.
- Keep route handlers thin; move business logic to service modules.
- Avoid files exceeding ~300 lines without explicit justification.
- Group by domain (`news`, `technical`, `strategy`) before technical type.

## Function and Method Standards

- Keep functions focused and side effects explicit.
- Prefer pure functions for scoring/decision logic.
- Validate inputs at boundaries (CLI args, API params, env parsing).
- Return typed domain objects instead of loosely structured dict/object maps where possible.

## Component Standards (Dashboard)

- Components should be presentational unless they own domain interaction.
- Shared formatting logic belongs in reusable utilities.
- Avoid duplicated display mappings across components.

## Commenting Philosophy

- Code should be self-explanatory first; comments should explain intent, constraints, or non-obvious trade-offs.
- Do not restate obvious code.
- Add short comments for financial/strategy assumptions and invariant rules.

## Error Handling Rules

- Never expose raw internal exceptions to API clients.
- Use consistent error envelopes with stable machine-readable error codes.
- Log context-rich internal errors (request ID, route, operation, external dependency).

## Logging Standards

- Use structured logs (JSON where possible).
- Never log secrets, webhook URLs, or sensitive keys.
- Include correlation/request IDs for API and ingest operations.

## Type Safety Expectations

- TypeScript strict mode is required; avoid `any` unless documented and isolated.
- Python: type annotate all new/changed public functions and core domain models.
- Prefer explicit schemas/types for external provider payload normalization.

## Python Style

- Follow PEP8 and consistent import ordering.
- Keep configuration parsing centralized and fail-fast for required settings.
- Use dataclasses/typed containers for core domain state where practical.
