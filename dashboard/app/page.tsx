import Link from "next/link";

import { NewsArticleCard } from "@/components/NewsArticleCard";
import { TechnicalLeaderboard } from "@/components/TechnicalLeaderboard";
import { getDashboardHeadlinesCached } from "@/lib/news/queries";
import { getLeaderboardSnapshotsCached } from "@/lib/technical/queries";

export default async function HomePage() {
  let headlines: Awaited<ReturnType<typeof getDashboardHeadlinesCached>> = [];
  let configError: string | null = null;
  try {
    headlines = await getDashboardHeadlinesCached(10);
  } catch (e) {
    configError = String(e);
  }

  let technicalRun: Awaited<ReturnType<typeof getLeaderboardSnapshotsCached>>["run"] = null;
  let technicalSnapshots: Awaited<ReturnType<typeof getLeaderboardSnapshotsCached>>["snapshots"] = [];
  let technicalError: string | null = null;
  try {
    const pack = await getLeaderboardSnapshotsCached();
    technicalRun = pack.run;
    technicalSnapshots = pack.snapshots;
  } catch (e) {
    technicalError = String(e);
  }

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="mt-2 max-w-3xl text-[var(--muted)]">
          <strong>Technical leaderboard</strong> below uses batch snapshots from Supabase (deterministic checklist scores,
          same Python engine as the alert bot). <strong>News</strong> is separate research-only context — it never
          changes technical scores.
        </p>
      </section>

      <section id="technical" className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Technical leaderboard</h2>
          <Link href="/leaderboard" className="text-sm">
            Full table →
          </Link>
        </div>
        {technicalError ? (
          <p className="text-sm text-amber-200">
            Could not load technical snapshots. ({technicalError})
          </p>
        ) : (
          <TechnicalLeaderboard run={technicalRun} snapshots={technicalSnapshots} compact />
        )}
      </section>

      <section className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">Market headlines</h2>
          <Link href="/market-news" className="text-sm">
            View all →
          </Link>
        </div>
        {configError ? (
          <p className="text-sm text-amber-200">
            Could not load headlines. Set <code className="rounded bg-zinc-900 px-1">NEXT_PUBLIC_SUPABASE_URL</code>{" "}
            and <code className="rounded bg-zinc-900 px-1">SUPABASE_SERVICE_ROLE_KEY</code>, apply the SQL migration,
            then ingest news. ({configError})
          </p>
        ) : headlines.length === 0 ? (
          <p className="text-sm text-[var(--muted)]">
            No rows yet. Run ingest:{" "}
            <code className="rounded bg-zinc-900 px-1">
              curl -H &quot;Authorization: Bearer $CRON_SECRET&quot; -X POST https://your-app/api/news/ingest
            </code>
          </p>
        ) : (
          <ul className="space-y-3">
            {headlines.map((h) => (
              <li key={h.id}>
                <NewsArticleCard item={h} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="text-sm text-[var(--muted)]">
        <p>
          Ingest route (secured): <code className="rounded bg-zinc-900 px-1">/api/news/ingest</code> · Cron schedule
          in <code className="rounded bg-zinc-900 px-1">vercel.json</code> · Logs in Supabase{" "}
          <code className="rounded bg-zinc-900 px-1">news_ingest_runs</code>.
        </p>
      </section>
    </div>
  );
}
