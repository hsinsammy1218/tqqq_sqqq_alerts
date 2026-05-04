import Link from "next/link";

import { NewsArticleCard } from "@/components/NewsArticleCard";
import { getDashboardHeadlinesCached } from "@/lib/news/queries";

export default async function HomePage() {
  let headlines: Awaited<ReturnType<typeof getDashboardHeadlinesCached>> = [];
  let configError: string | null = null;
  try {
    headlines = await getDashboardHeadlinesCached(10);
  } catch (e) {
    configError = String(e);
  }

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="mt-2 max-w-3xl text-[var(--muted)]">
          Technical analytics live in your existing routes (wire `stock_snapshots`, `dashboard_runs`, etc.). This
          layer adds <strong>research-only news context</strong> — confirmation, catalyst, and narrative-risk hints —
          without changing signal math.
        </p>
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
