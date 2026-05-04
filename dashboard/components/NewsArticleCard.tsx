import type { NewsListItem } from "@/lib/news/pagination";

function sentimentLabel(s: number | null): string {
  if (s == null) return "—";
  if (s <= -0.25) return "Bearish lean";
  if (s >= 0.25) return "Bullish lean";
  return "Neutral";
}

export function NewsArticleCard({ item }: { item: NewsListItem }) {
  const chips = [...item.related_tickers.slice(0, 6), ...item.related_sectors.slice(0, 4)];
  return (
    <article className="rounded-lg border border-zinc-800 bg-zinc-950 p-4 shadow-sm">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-[var(--muted)]">
        <span>{new Date(item.published_at).toLocaleString()}</span>
        <span>·</span>
        <span>{item.source}</span>
        <span>·</span>
        <span>Importance {Math.round(item.importance_score)}</span>
        {item.category ? (
          <>
            <span>·</span>
            <span className="rounded bg-zinc-800 px-1.5 py-0.5">{item.category}</span>
          </>
        ) : null}
        <span
          className={`rounded px-1.5 py-0.5 ${
            (item.sentiment ?? 0) <= -0.25
              ? "bg-red-950 text-red-200"
              : (item.sentiment ?? 0) >= 0.25
                ? "bg-emerald-950 text-emerald-200"
                : "bg-zinc-800 text-zinc-300"
          }`}
        >
          {sentimentLabel(item.sentiment)}
        </span>
      </div>
      <h3 className="text-lg font-semibold leading-snug text-zinc-100">
        <a href={item.url} target="_blank" rel="noopener noreferrer" className="hover:underline">
          {item.headline}
        </a>
      </h3>
      {item.summary ? <p className="mt-2 text-sm text-[var(--muted)] line-clamp-3">{item.summary}</p> : null}
      {chips.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {chips.map((c) => (
            <span key={c} className="rounded border border-zinc-700 px-1.5 py-0.5 text-xs text-zinc-300">
              {c}
            </span>
          ))}
        </div>
      ) : null}
    </article>
  );
}
