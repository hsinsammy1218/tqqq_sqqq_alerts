import type { NormalizedArticle } from "../types";
import { fetchFinnhubCompanyNews, fetchFinnhubGeneral } from "./finnhub";

export async function ingestFromProvider(
  provider: string,
  apiKey: string,
  tickerSymbols: string[],
): Promise<NormalizedArticle[]> {
  const out: NormalizedArticle[] = [];
  if (!apiKey.trim()) return out;

  if (provider === "finnhub") {
    const general = await fetchFinnhubGeneral(apiKey, 45);
    out.push(...general);
    const syms = [...new Set(tickerSymbols.map((s) => s.toUpperCase()))].slice(0, 12);
    for (const sym of syms) {
      try {
        const rows = await fetchFinnhubCompanyNews(apiKey, sym, 12);
        out.push(...rows);
      } catch {
        /* rate limit / symbol miss — continue */
      }
      await sleep(350);
    }
    return dedupeArticles(out);
  }

  throw new Error(`Unknown NEWS_PROVIDER: ${provider}`);
}

function dedupeArticles(rows: NormalizedArticle[]): NormalizedArticle[] {
  const seen = new Set<string>();
  const uniq: NormalizedArticle[] = [];
  for (const r of rows) {
    if (seen.has(r.url)) continue;
    seen.add(r.url);
    uniq.push(r);
  }
  return uniq;
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}
