import Link from "next/link";

import type { DashboardRunMeta, StockSnapshotRow } from "@/lib/technical/types";
import { regimeClass } from "@/lib/ui/regimeClass";

export function TechnicalLeaderboard(props: {
  run: DashboardRunMeta | null;
  snapshots: StockSnapshotRow[];
  compact?: boolean;
}) {
  const { run, snapshots, compact } = props;
  const rows = compact ? snapshots.slice(0, 12) : snapshots;

  if (!run || snapshots.length === 0) {
    return (
      <p className="text-sm text-[var(--muted)]">
        No technical snapshots yet. Apply the Supabase migration for{" "}
        <code className="rounded bg-zinc-900 px-1">dashboard_runs</code> /{" "}
        <code className="rounded bg-zinc-900 px-1">stock_snapshots</code>, set Supabase env vars, then run{" "}
        <code className="rounded bg-zinc-900 px-1">python publish_technical_dashboard.py</code> from the repo root.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--muted)]">
        Latest batch <span className="text-zinc-300">{run.started_at}</span>
        {run.scorer_version ? (
          <>
            {" "}
            · scorer <code className="rounded bg-zinc-900 px-1">{run.scorer_version}</code>
          </>
        ) : null}{" "}
        · {run.tickers_succeeded}/{run.tickers_requested} symbols · Sorted by dominance confidence (research-only; not
        trade advice).
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-[var(--muted)]">
              <th className="py-2 pr-3 font-medium">#</th>
              <th className="py-2 pr-3 font-medium">Symbol</th>
              <th className="py-2 pr-3 font-medium">Confidence</th>
              <th className="py-2 pr-3 font-medium">Bull</th>
              <th className="py-2 pr-3 font-medium">Bear</th>
              <th className="py-2 pr-3 font-medium">Regime</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={row.id} className="border-b border-zinc-900">
                <td className="py-2 pr-3 text-[var(--muted)]">{idx + 1}</td>
                <td className="py-2 pr-3 font-medium">
                  <Link href={`/stock/${encodeURIComponent(row.symbol)}`} className="text-[var(--text)] hover:underline">
                    {row.symbol}
                  </Link>
                </td>
                <td className="py-2 pr-3">{row.confidence}</td>
                <td className="py-2 pr-3">{row.bull_strength}</td>
                <td className="py-2 pr-3">{row.bear_strength}</td>
                <td className={`py-2 pr-3 capitalize ${regimeClass(row.regime)}`}>{row.regime.replace("_", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {compact && snapshots.length > rows.length ? (
        <p className="text-sm">
          <Link href="/leaderboard" className="text-[var(--text)] underline">
            Full leaderboard ({snapshots.length} symbols)
          </Link>
        </p>
      ) : null}
    </div>
  );
}
