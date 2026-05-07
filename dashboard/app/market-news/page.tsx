import Link from "next/link";

import { NewsArticleCard } from "@/components/NewsArticleCard";
import { getMarketNewsCached } from "@/lib/news/queries";

const SECTOR_FILTERS = [
  "",
  "technology",
  "financials",
  "energy",
  "etf_broad_market",
  "etf_broad_tech",
  "etf_sector_tech",
  "etf_sector_financials",
  "etf_sector_energy",
];

interface PageProps {
  searchParams: Promise<{ page?: string; hours?: string; sector?: string }>;
}

export default async function MarketNewsPage({ searchParams }: PageProps) {
  const sp = await searchParams;
  const page = Math.max(0, Number(sp.page ?? 0) || 0);
  const hours = Math.min(168, Math.max(6, Number(sp.hours ?? 72) || 72));
  const sector = sp.sector?.trim() || null;

  let items: Awaited<ReturnType<typeof getMarketNewsCached>> = [];
  let err: string | null = null;
  try {
    items = await getMarketNewsCached({ hours, sector, page, limit: 15 });
  } catch (e) {
    err = String(e);
  }

  const limit = 15;
  const hasMore = items.length === limit;

  function buildQs(next: { page?: number; hours?: number; sector?: string | null }) {
    const p = new URLSearchParams();
    p.set("hours", String(next.hours ?? hours));
    p.set("page", String(next.page ?? page));
    const sec = next.sector !== undefined ? next.sector : sector;
    if (sec) p.set("sector", sec);
    return `?${p.toString()}`;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Market news</h1>
        <p className="mt-1 text-sm text-[var(--muted)]">
          Paginated feed (importance × recency). News is contextual only — not blended into technical scores.
        </p>
      </div>

      <div className="flex flex-wrap gap-2 text-sm">
        <span className="text-[var(--muted)]">Hours:</span>
        {[24, 72, 168].map((h) => (
          <Link
            key={h}
            href={buildQs({ page: 0, hours: h, sector })}
            className={`rounded border px-2 py-1 ${hours === h ? "border-[var(--accent)] text-[var(--accent)]" : "border-zinc-600"}`}
          >
            {h}h
          </Link>
        ))}
      </div>

      <div className="flex flex-wrap gap-2 text-sm">
        <span className="text-[var(--muted)]">Sector tag:</span>
        {SECTOR_FILTERS.map((s) => (
          <Link
            key={s || "all"}
            href={buildQs({ page: 0, hours, sector: s || null })}
            className={`rounded border px-2 py-1 ${(sector ?? "") === s ? "border-[var(--accent)] text-[var(--accent)]" : "border-zinc-600"}`}
          >
            {s || "all"}
          </Link>
        ))}
      </div>

      {err ? (
        <p className="text-amber-200">{err}</p>
      ) : (
        <ul className="space-y-4">
          {items.map((item) => (
            <li key={item.id}>
              <NewsArticleCard item={item} />
            </li>
          ))}
        </ul>
      )}

      <div className="flex gap-4 text-sm">
        {page > 0 ? (
          <Link href={buildQs({ page: page - 1, hours, sector })}>← Previous</Link>
        ) : null}
        {hasMore ? (
          <Link href={buildQs({ page: page + 1, hours, sector })}>Next →</Link>
        ) : null}
      </div>
    </div>
  );
}
