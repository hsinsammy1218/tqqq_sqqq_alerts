import {
  extractTickers,
  heuristicSentiment,
  importanceScore,
  inferCategory,
  sectorsForTickers,
} from "./enrichment";
import type { MarketNewsRow, NormalizedArticle } from "./types";

export function finalizeArticle(
  raw: NormalizedArticle,
  allowlistEnv?: string,
): MarketNewsRow {
  const mergedText = `${raw.headline} ${raw.summary ?? ""}`;
  const extracted = extractTickers(mergedText, allowlistEnv);
  const tickers = [...new Set([...raw.related_tickers, ...extracted])].slice(0, 10);
  const sectors = [
    ...new Set([...raw.related_sectors, ...sectorsForTickers(tickers)]),
  ].slice(0, 6);
  const category = raw.category ?? inferCategory({ ...raw, related_tickers: tickers });
  const article: NormalizedArticle = {
    ...raw,
    related_tickers: tickers,
    related_sectors: sectors,
    category,
  };
  const sent = heuristicSentiment(article.headline, article.summary);
  const pubMs = Date.parse(article.published_at);
  const importance = importanceScore(article, Number.isFinite(pubMs) ? pubMs : Date.now());

  return {
    ...article,
    sentiment: sent.value,
    sentiment_model: sent.model,
    importance_score: importance,
  };
}
