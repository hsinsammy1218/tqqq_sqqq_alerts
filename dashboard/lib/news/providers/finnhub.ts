import { normalizeNewsUrl } from "../normalize";
import type { NewsCategory, NormalizedArticle } from "../types";

interface FinnhubNewsItem {
  category?: string;
  datetime?: number;
  headline?: string;
  id?: number;
  image?: string;
  related?: string;
  source?: string;
  summary?: string;
  url?: string;
}

function mapFinnhubCategory(cat: string | undefined): NewsCategory | null {
  if (!cat) return "market";
  const c = cat.toLowerCase();
  if (c === "mergers") return "market";
  return "market";
}

export async function fetchFinnhubGeneral(apiKey: string, limit = 40): Promise<NormalizedArticle[]> {
  const u = new URL("https://finnhub.io/api/v1/news");
  u.searchParams.set("category", "general");
  u.searchParams.set("token", apiKey);
  const res = await fetch(u.toString(), { next: { revalidate: 0 } });
  if (!res.ok) {
    throw new Error(`Finnhub general news ${res.status}: ${await res.text()}`);
  }
  const data = (await res.json()) as FinnhubNewsItem[];
  const slice = Array.isArray(data) ? data.slice(0, limit) : [];
  return slice.map((item) => toArticle(item, mapFinnhubCategory(item.category))).filter(Boolean) as NormalizedArticle[];
}

export async function fetchFinnhubCompanyNews(
  apiKey: string,
  symbol: string,
  limit = 15,
): Promise<NormalizedArticle[]> {
  const now = new Date();
  const from = new Date(now.getTime() - 7 * 24 * 3600 * 1000);
  const u = new URL("https://finnhub.io/api/v1/company-news");
  u.searchParams.set("symbol", symbol.toUpperCase());
  u.searchParams.set("from", from.toISOString().slice(0, 10));
  u.searchParams.set("to", now.toISOString().slice(0, 10));
  u.searchParams.set("token", apiKey);
  const res = await fetch(u.toString(), { next: { revalidate: 0 } });
  if (!res.ok) {
    throw new Error(`Finnhub company news ${res.status}: ${await res.text()}`);
  }
  const data = (await res.json()) as FinnhubNewsItem[];
  const slice = Array.isArray(data) ? data.slice(0, limit) : [];
  return slice
    .map((item) =>
      toArticle(
        item,
        "ticker",
        symbol.toUpperCase(),
      ),
    )
    .filter(Boolean) as NormalizedArticle[];
}

function toArticle(
  item: FinnhubNewsItem,
  category: NewsCategory | null,
  forcedTicker?: string,
): NormalizedArticle | null {
  const headline = item.headline?.trim();
  const urlRaw = item.url?.trim();
  if (!headline || !urlRaw) return null;
  const tsSec = item.datetime ?? 0;
  const published_at = new Date(tsSec * 1000).toISOString();
  const related = forcedTicker
    ? [forcedTicker]
    : parseRelated(item.related);
  return {
    external_id: item.id != null ? String(item.id) : null,
    headline,
    summary: item.summary?.trim() ?? null,
    source: item.source?.trim() ?? "finnhub",
    url: normalizeNewsUrl(urlRaw),
    published_at,
    related_tickers: related,
    related_sectors: [],
    category,
    raw_payload: item as unknown as Record<string, unknown>,
  };
}

function parseRelated(related: string | undefined): string[] {
  if (!related?.trim()) return [];
  return related
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean)
    .slice(0, 8);
}
