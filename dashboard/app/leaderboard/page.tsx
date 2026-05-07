import Link from "next/link";

import { TechnicalLeaderboard } from "@/components/TechnicalLeaderboard";
import { getLeaderboardSnapshotsCached } from "@/lib/technical/queries";

export default async function LeaderboardPage() {
  let error: string | null = null;
  let run: Awaited<ReturnType<typeof getLeaderboardSnapshotsCached>>["run"] = null;
  let snapshots: Awaited<ReturnType<typeof getLeaderboardSnapshotsCached>>["snapshots"] = [];
  try {
    const pack = await getLeaderboardSnapshotsCached();
    run = pack.run;
    snapshots = pack.snapshots;
  } catch (e) {
    error = String(e);
  }

  return (
    <div className="space-y-6">
      <div>
        <p className="text-sm text-[var(--muted)]">
          <Link href="/">Home</Link> / Leaderboard
        </p>
        <h1 className="mt-2 text-2xl font-bold">Technical leaderboard</h1>
        <p className="mt-1 max-w-3xl text-sm text-[var(--muted)]">
          Ranked by stack-dominance confidence (0–100) from the weighted daily + 4h checklist. Research-only; not
          investment advice.
        </p>
      </div>

      <section className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        {error ? (
          <p className="text-sm text-amber-200">{error}</p>
        ) : (
          <TechnicalLeaderboard run={run} snapshots={snapshots} />
        )}
      </section>
    </div>
  );
}
