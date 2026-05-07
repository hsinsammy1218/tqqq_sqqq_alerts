# Performance Review

Last reviewed: 2026-05-06

## Current Performance Posture

The system is likely sufficient for low-to-moderate current load, but some architecture choices will become bottlenecks as symbol counts, ingest volume, and API traffic increase.

## Key Bottlenecks

| Severity | Area | Finding | Impact |
|---|---|---|---|
| Medium | News ingest | External provider collection is still mostly serial | Throughput bottleneck as symbol/news volume grows |
| Medium | Provider enrichment | Serial external lookups with delays | Slow ingest throughput |
| Low | Core Python modules | Main strategy/runtime split improved modularity | Remaining large research modules still harder to profile |
| Low | Dashboard list endpoints | Potential growth in response payload sizes | Increased transfer/render costs |
| Low | Cache policy | Current intervals static; no load-adaptive strategy | Inefficient freshness/perf trade-off over time |

## Database and Query Observations

- Supabase query patterns are generally scoped with limits and filters.
- Migrations indicate index awareness for news and snapshot retrieval.
- Current leaderboard retrieval strategy is acceptable for constrained universe sizes.

## Rendering and Frontend Considerations

- Caching and tag revalidation patterns are in place and useful.
- Duplicate presentation helper logic has been reduced via shared utilities.
- As data volume grows, pagination and selective field projection should be tightened.

## Scaling Concerns

1. Ingest pipeline remains constrained by mostly serial provider fetching.
2. Ingest service extraction improved boundaries, but background execution is still not implemented.
3. Telemetry baseline exists (request IDs + duration headers/logs), but metrics backend/alerts remain missing.

## Recommended Improvements

## Quick Wins

- Conflict-aware DB operations are now in place for ingest upsert path.
- Normalize API payload fields and cap defaults conservatively.
- Endpoint timing/request IDs are now emitted; next step is persistent metric aggregation.

## Medium-Term

- Move ingest into service modules and batch execution primitives.
- Introduce optional asynchronous job processing for large ingest runs.
- Profile and isolate Python scoring/indicator hotspots with benchmark fixtures.

## Long-Term

- Define service SLOs for ingest duration and API latency.
- Introduce observability tooling (metrics + traces) across both Python and Next.js runtime paths.
- Create capacity plan thresholds for symbol count/news item growth.

## Suggested KPIs

| KPI | Target |
|---|---|
| `/api/news/ingest` p95 duration | under agreed cron window |
| `/api/news/market` p95 latency | < 300 ms (cached path target) |
| Dashboard page p95 render | < 1.5 s |
| Ingest failure rate | < 1% per run |
| Technical publish end-to-end duration | stable trend with symbol count growth |
