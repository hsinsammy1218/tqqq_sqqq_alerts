import { unstable_cache } from "next/cache";

import { createServiceClient } from "@/lib/supabase/service";

import { toListItem, type NewsListItem } from "./pagination";

export interface MarketNewsQueryParams {
  hours: number;
  sector: string | null;
  page: number;
  limit: number;
}

async function queryMarketNewsInternal(params: MarketNewsQueryParams): Promise<NewsListItem[]> {
  const supabase = createServiceClient();
  const hours = Math.min(168, Math.max(1, params.hours));
  const limit = Math.min(50, Math.max(1, params.limit));
  const page = Math.max(0, params.page);
  const cutoff = new Date(Date.now() - hours * 3600 * 1000).toISOString();

  let q = supabase
    .from("market_news")
    .select(
      "id, headline, summary, source, url, published_at, sentiment, importance_score, related_tickers, related_sectors, category",
    )
    .gte("published_at", cutoff)
    .order("importance_score", { ascending: false })
    .order("published_at", { ascending: false })
    .order("id", { ascending: false });

  if (params.sector?.trim()) {
    q = q.contains("related_sectors", [params.sector.trim()]);
  }

  const from = page * limit;
  const to = from + limit - 1;
  const { data, error } = await q.range(from, to);
  if (error) throw new Error(error.message);
  return (data ?? []).map((row) => toListItem(row as Record<string, unknown>));
}

/** Cached list for App Router pages (tag `news` invalidated by ingest). */
export function getMarketNewsCached(params: MarketNewsQueryParams) {
  return unstable_cache(
    async () => queryMarketNewsInternal(params),
    [
      "market-news",
      String(params.hours),
      params.sector ?? "",
      String(params.page),
      String(params.limit),
    ],
    { tags: ["news"], revalidate: 90 },
  )();
}

/** Direct query for Route Handlers (no implicit cache layer). */
export async function queryMarketNews(params: MarketNewsQueryParams): Promise<NewsListItem[]> {
  return queryMarketNewsInternal(params);
}

export async function queryTickerNews(
  symbol: string,
  hours: number,
  page: number,
  limit: number,
): Promise<NewsListItem[]> {
  const supabase = createServiceClient();
  const h = Math.min(168, Math.max(1, hours));
  const lim = Math.min(50, Math.max(1, limit));
  const pg = Math.max(0, page);
  const sym = symbol.trim().toUpperCase();
  const cutoff = new Date(Date.now() - h * 3600 * 1000).toISOString();

  const { data, error } = await supabase
    .from("market_news")
    .select(
      "id, headline, summary, source, url, published_at, sentiment, importance_score, related_tickers, related_sectors, category",
    )
    .gte("published_at", cutoff)
    .contains("related_tickers", [sym])
    .order("importance_score", { ascending: false })
    .order("published_at", { ascending: false })
    .order("id", { ascending: false })
    .range(pg * lim, pg * lim + lim - 1);

  if (error) throw new Error(error.message);
  return (data ?? []).map((row) => toListItem(row as Record<string, unknown>));
}

export function getTickerNewsCached(symbol: string, hours: number, page: number, limit: number) {
  const sym = symbol.trim().toUpperCase();
  return unstable_cache(
    async () => queryTickerNews(sym, hours, page, limit),
    ["ticker-news", sym, String(hours), String(page), String(limit)],
    { tags: ["news"], revalidate: 90 },
  )();
}

export async function queryHeadlinesForDashboard(limit = 8): Promise<NewsListItem[]> {
  return queryMarketNewsInternal({
    hours: 36,
    sector: null,
    page: 0,
    limit,
  });
}

export function getDashboardHeadlinesCached(limit = 8) {
  return unstable_cache(
    async () => queryHeadlinesForDashboard(limit),
    ["dashboard-headlines", String(limit)],
    { tags: ["news"], revalidate: 90 },
  )();
}
