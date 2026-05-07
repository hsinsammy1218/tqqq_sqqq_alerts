# Architecture Rules

## Layering Rules

- Use clear layers: **interface (CLI/API/UI)** -> **application services** -> **domain logic** -> **infrastructure adapters**.
- Domain logic must not depend directly on framework/runtime APIs.
- Route handlers and CLI entrypoints orchestrate only; they do not implement core business rules.
- Maintain facade compatibility when splitting modules (e.g., legacy import surface via re-export module).

## Dependency Direction

- Dependencies flow inward toward domain, never outward.
- Domain modules cannot import Next.js route helpers, HTTP types, or Supabase clients directly.
- Shared utilities cannot import high-level feature modules.

## Service Boundaries

- Separate news ingest, technical scoring, and persistence concerns.
- Each service module must expose a narrow interface and typed contracts.
- External providers (KlickAnalytics, Finnhub) must be wrapped behind adapter interfaces.
- Ingest write paths must use conflict-aware persistence and idempotent behavior by default.

## State Management Standards

- Persistent state transitions must be explicit and auditable.
- Position state updates should be centralized to avoid contradictory transitions.
- Cache invalidation triggers must be colocated with write operations and documented.

## Modularity Requirements

- Avoid god files; split modules exceeding complexity thresholds.
- No duplicate helpers across routes/components; use shared modules.
- Keep formatting/presentation logic separate from policy logic.

## Scalability Principles

- Prefer idempotent operations for ingest/publish workflows.
- Design ingest paths to tolerate retries and duplicate events.
- Long-running or high-volume tasks should be background-job capable.

## Architectural Governance

- Significant architecture changes require an ADR update.
- Every PR touching core strategy, ingest, or persistence must include impact notes:
  - boundaries touched
  - migration risk
  - rollback plan.
