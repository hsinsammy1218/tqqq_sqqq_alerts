import type { MarketNewsRow } from "./types";

export interface NewsCursor {
  t: string;
  id: string;
}

export function encodeNewsCursor(row: { published_at: string; id: string }): string {
  const payload: NewsCursor = { t: row.published_at, id: row.id };
  return Buffer.from(JSON.stringify(payload)).toString("base64url");
}

export function decodeNewsCursor(raw: string | null): NewsCursor | null {
  if (!raw?.trim()) return null;
  try {
    const json = Buffer.from(raw, "base64url").toString("utf8");
    const o = JSON.parse(json) as NewsCursor;
    if (typeof o.t === "string" && typeof o.id === "string") return o;
    return null;
  } catch {
    return null;
  }
}

/** Client-safe slice of a news row */
export type NewsListItem = Pick<
  MarketNewsRow,
  | "headline"
  | "summary"
  | "source"
  | "url"
  | "published_at"
  | "sentiment"
  | "importance_score"
  | "related_tickers"
  | "related_sectors"
  | "category"
> & { id: string };

export function toListItem(row: Record<string, unknown>): NewsListItem {
  return {
    id: String(row.id),
    headline: String(row.headline),
    summary: row.summary != null ? String(row.summary) : null,
    source: String(row.source ?? ""),
    url: String(row.url),
    published_at: String(row.published_at),
    sentiment: row.sentiment != null ? Number(row.sentiment) : null,
    importance_score: Number(row.importance_score ?? 0),
    related_tickers: (row.related_tickers as string[]) ?? [],
    related_sectors: (row.related_sectors as string[]) ?? [],
    category: (row.category as MarketNewsRow["category"]) ?? null,
  };
}
