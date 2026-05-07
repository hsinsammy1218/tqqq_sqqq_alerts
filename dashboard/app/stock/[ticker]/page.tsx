import Link from "next/link";
import { notFound } from "next/navigation";

import { NewsArticleCard } from "@/components/NewsArticleCard";
import { TechnicalBreakdownPanel } from "@/components/TechnicalBreakdownPanel";
import { computeNewsContextFlags } from "@/lib/news/riskFlags";
import { getTickerNewsCached } from "@/lib/news/queries";
import { getTickerSnapshotCached } from "@/lib/technical/queries";

interface PageProps {
  params: Promise<{ ticker: string }>;
}

const TICKER_RE = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/;

export default async function StockNewsPage({ params }: PageProps) {
  const { ticker } = await params;
  const raw = ticker.trim();
  if (!TICKER_RE.test(raw)) notFound();
  const symbol = raw.toUpperCase();

  let items: Awaited<ReturnType<typeof getTickerNewsCached>> = [];
  let err: string | null = null;
  try {
    items = await getTickerNewsCached(symbol, 72, 0, 25);
  } catch (e) {
    err = String(e);
  }

  const flags = computeNewsContextFlags(
    items.map((i) => ({
      sentiment: i.sentiment,
      headline: i.headline,
      summary: i.summary,
    })),
  );

  const catalyst = flags.catalystSoon;
  const risk = flags.narrativeRisk;

  let techRun: Awaited<ReturnType<typeof getTickerSnapshotCached>>["run"] = null;
  let techSnapshot: Awaited<ReturnType<typeof getTickerSnapshotCached>>["snapshot"] = null;
  let techConfigError: string | null = null;
  try {
    const pack = await getTickerSnapshotCached(symbol);
    techRun = pack.run;
    techSnapshot = pack.snapshot;
  } catch (e) {
    techConfigError = String(e);
  }

  return (
    <div className="space-y-6">
      <div>
        <p className="text-sm text-[var(--muted)]">
          <Link href="/">Home</Link> / Stock context
        </p>
        <h1 className="mt-2 text-2xl font-bold">{symbol}</h1>
        <p className="mt-1 text-sm text-[var(--muted)]">
          Technical snapshot (batch scores from Supabase) plus news context for research (last ~72h in DB). News does
          not affect technical numbers.
        </p>
      </div>

      <TechnicalBreakdownPanel symbol={symbol} run={techRun} snapshot={techSnapshot} configError={techConfigError} />

      <div className="flex flex-wrap gap-2">
        <span
          className={`rounded px-2 py-1 text-xs font-medium ${risk ? "bg-amber-950 text-amber-100" : "bg-zinc-800 text-zinc-400"}`}
        >
          Narrative risk: {risk ? "yes — review headlines" : "low in window"}
        </span>
        <span
          className={`rounded px-2 py-1 text-xs font-medium ${catalyst ? "bg-indigo-950 text-indigo-100" : "bg-zinc-800 text-zinc-400"}`}
        >
          Catalyst cue: {catalyst ? "possible — check dates" : "none flagged"}
        </span>
      </div>

      <p className="text-sm text-[var(--muted)]">{flags.summary}</p>

      <h2 className="text-lg font-semibold">News context</h2>

      {err ? (
        <p className="text-amber-200">{err}</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">
          No ingested articles tagged with {symbol}. Ensure ingest ran and FINNHUB returned company news for this
          symbol.
        </p>
      ) : (
        <section>
          <h2 className="mb-3 text-lg font-semibold">Headlines</h2>
          <ul className="space-y-4">
            {items.map((item) => (
              <li key={item.id}>
                <NewsArticleCard item={item} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
