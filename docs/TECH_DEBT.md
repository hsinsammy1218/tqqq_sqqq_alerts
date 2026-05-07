# Technical Debt Register

Last reviewed: 2026-05-06

## Debt Inventory

| ID | Debt Item | Severity | Impact | Recommended Fix | Complexity |
|---|---|---|---|---|---|
| TD-001 | Large research/orchestration modules (`backtest.py`, `backtest_sweep.py`, `walk_forward.py`) | Medium | Slower changes, regression risk in research workflow | Continue split by domain/application boundaries | High |
| TD-002 | Limited automated test breadth across strategy and routes | Medium | Undetected behavior drift in less-covered transitions/edges | Expand matrix/integration suite with CI enforcement | Medium |
| TD-003 | CI security scanning depth is minimal | Medium | Dependency risk may not be caught early enough | Add audit/scan steps to CI and policy gates | Medium |
| TD-004 | API contract versioning not yet introduced | Medium | Future breaking changes can impact clients | Introduce `/api/v1` and migration policy | Medium |
| TD-005 | Shared-secret-only route auth model | Medium | Coarse-grained authorization for future multi-user operation | Introduce scoped authn/authz if usage expands | Medium |
| TD-006 | Observability lacks centralized metrics backend/alerts | Medium | Harder incident detection and SLO management | Add metrics sink and alert thresholds | Medium |
| TD-007 | Some route handlers still mix validation/query logic | Medium | Harder route testing and evolution | Continue service extraction pattern | Medium |
| TD-008 | Inconsistent API response/error schema | Medium | Higher frontend complexity | Define contract and typed response wrappers | Medium |
| TD-009 | Broad service-role query usage surface | Medium | Increased misuse/blast radius risk | Restrict service-role client to server-only adapters | Medium |
| TD-010 | Unpinned Python dependencies | Low | Reproducibility and patch uncertainty | Pin versions + scheduled update cadence | Low |

## Debt by Category

### Architecture Debt

- Overloaded files that combine control flow, domain policy, and formatting.
- Incomplete layering in dashboard API handlers.

### Quality Debt

- Lack of a repeatable test strategy and quality gates.
- Limited contract tests for API outputs and ingest behavior.

### Security Debt

- Raw internal errors exposed to clients.
- Secret-based route auth is functional but coarse for future multi-user workflows.

### Operational Debt

- Missing dashboard health endpoint and lightweight metrics.
- No codified release pipeline.

## Debt Prioritization Matrix

| Priority | Items |
|---|---|
| Immediate | TD-002, TD-003 |
| Near-term | TD-001, TD-004, TD-006, TD-007 |
| Scheduled | TD-005, TD-008, TD-009, TD-010 |

## Recommended Ownership

- **Core strategy maintainers:** TD-001, TD-002
- **Platform/devops owner:** TD-003, TD-010
- **API/dashboard maintainers:** TD-004, TD-005, TD-007, TD-008, TD-009
- **Frontend maintainers:** TD-006

## Review Cadence

- Re-score debt severity monthly.
- Review debt burn-down in sprint planning.
- Require debt impact section in every architecture-impacting PR.
