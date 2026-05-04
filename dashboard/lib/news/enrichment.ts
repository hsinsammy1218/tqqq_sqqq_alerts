import type { NewsCategory, NormalizedArticle } from "./types";

/** Minimal ticker → GICS-like slug map for research tagging (expand over time). */
const TICKER_SECTOR: Record<string, string> = {
  AAPL: "technology",
  MSFT: "technology",
  GOOGL: "communication_services",
  GOOG: "communication_services",
  AMZN: "consumer_discretionary",
  META: "communication_services",
  NVDA: "technology",
  TSLA: "consumer_discretionary",
  AVGO: "technology",
  COST: "consumer_staples",
  NFLX: "communication_services",
  QQQ: "etf_broad_tech",
  SPY: "etf_broad_market",
  DIA: "etf_broad_market",
  IWM: "etf_small_cap",
  SMH: "etf_semis",
  XLK: "etf_sector_tech",
  XLF: "etf_sector_financials",
  XLE: "etf_sector_energy",
  JPM: "financials",
  BAC: "financials",
  XOM: "energy",
};

const DEFAULT_ALLOWLIST = new Set(
  Object.keys(TICKER_SECTOR).concat(["AMD", "INTC", "COIN", "SQ", "HOOD"]),
);

const TICKER_RE = /\b([A-Z]{1,5})\b/g;

const BEARISH = /\b(downgrade|miss|investigation|sec probe|lawsuit|layoff|warning|bankruptcy|crash|selloff)\b/i;
const BULLISH = /\b(upgrade|beat|raise guidance|record|breakthrough|approval|surge|rally)\b/i;

const IMPORTANCE_KEYWORDS: { re: RegExp; add: number }[] = [
  { re: /\bearnings\b/i, add: 25 },
  { re: /\bguidance\b/i, add: 15 },
  { re: /\bFed\b|\bFOMC\b|\bPowell\b/i, add: 20 },
  { re: /\bFDA\b|\bclinical\b/i, add: 18 },
  { re: /\bdowngrade\b|\bupgrade\b/i, add: 12 },
  { re: /\bSEC\b|\binvestigation\b/i, add: 15 },
];

function parseAllowlist(env: string | undefined): Set<string> {
  if (!env?.trim()) return DEFAULT_ALLOWLIST;
  return new Set(
    env
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
  );
}

export function extractTickers(text: string, allowlistEnv?: string): string[] {
  const allow = parseAllowlist(allowlistEnv);
  const found = new Set<string>();
  let m: RegExpExecArray | null;
  const upper = text.toUpperCase();
  TICKER_RE.lastIndex = 0;
  while ((m = TICKER_RE.exec(upper)) !== null) {
    const sym = m[1];
    if (allow.has(sym)) found.add(sym);
    if (found.size >= 8) break;
  }
  return [...found];
}

export function sectorsForTickers(tickers: string[]): string[] {
  const s = new Set<string>();
  for (const t of tickers) {
    const sec = TICKER_SECTOR[t];
    if (sec) s.add(sec);
  }
  return [...s];
}

/** Rule-based sentiment -1..1; news does not affect technical scores. */
export function heuristicSentiment(headline: string, summary: string | null): {
  value: number;
  model: string;
} {
  const blob = `${headline} ${summary ?? ""}`;
  let score = 0;
  if (BEARISH.test(blob)) score -= 0.55;
  if (BULLISH.test(blob)) score += 0.45;
  score = Math.max(-1, Math.min(1, score));
  if (score === 0) return { value: 0, model: "rule_lexicon_neutral" };
  return { value: score, model: "rule_lexicon_v1" };
}

/** 0–100 importance; recency applied at query time optionally; here keyword/static boost only. */
export function importanceScore(
  article: NormalizedArticle,
  publishedAtMs: number,
): number {
  let score = 40;
  const blob = `${article.headline} ${article.summary ?? ""}`;
  for (const { re, add } of IMPORTANCE_KEYWORDS) {
    if (re.test(blob)) score += add;
  }
  if (article.category === "earnings") score += 20;
  const ageH = (Date.now() - publishedAtMs) / 3_600_000;
  score -= Math.min(25, ageH * 2);
  return Math.round(Math.max(0, Math.min(100, score)));
}

export function inferCategory(
  article: Pick<NormalizedArticle, "headline" | "summary" | "related_tickers">,
): NewsCategory | null {
  const blob = `${article.headline} ${article.summary ?? ""}`;
  if (/\bearnings\b|\bEPS\b|\bguidance\b|\bquarterly\b/i.test(blob)) return "earnings";
  if (/\bFed\b|\bFOMC\b|\binflation\b|\bCPI\b|\bjobs report\b/i.test(blob)) return "macro";
  if (article.related_tickers.length > 0) return "ticker";
  if (/\bsector\b|\bETF\b|\bXLK\b|\bXLF\b/i.test(blob)) return "sector";
  return "market";
}
