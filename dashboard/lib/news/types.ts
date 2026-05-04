export type NewsCategory =
  | "market"
  | "sector"
  | "ticker"
  | "earnings"
  | "macro";

export interface NormalizedArticle {
  external_id: string | null;
  headline: string;
  summary: string | null;
  source: string;
  url: string;
  published_at: string;
  related_tickers: string[];
  related_sectors: string[];
  category: NewsCategory | null;
  raw_payload: Record<string, unknown> | null;
}

export interface MarketNewsRow extends NormalizedArticle {
  sentiment: number | null;
  sentiment_model: string | null;
  importance_score: number;
}
