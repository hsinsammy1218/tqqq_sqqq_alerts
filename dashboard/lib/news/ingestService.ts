import type { SupabaseClient } from "@supabase/supabase-js";

import { finalizeArticle } from "@/lib/news/finalizeArticle";
import { ingestFromProvider } from "@/lib/news/providers";

interface IngestSummary {
  provider: string;
  items_fetched: number;
  items_inserted: number;
  items_deduped: number;
  status: "success" | "partial" | "failed";
  run_id: string;
}

export async function runNewsIngest(
  supabase: SupabaseClient,
  config: {
    provider: string;
    apiKey: string;
    tickers: string[];
    allowlist?: string;
  },
): Promise<IngestSummary> {
  const runInsert = await supabase
    .from("news_ingest_runs")
    .insert({
      status: "running",
      provider: config.provider,
      items_fetched: 0,
      items_inserted: 0,
      items_deduped: 0,
    })
    .select("id")
    .single();

  const runId = runInsert.data?.id as string | undefined;
  if (runInsert.error || !runId) {
    throw new Error(`Could not start ingest run: ${runInsert.error?.message ?? "unknown"}`);
  }

  let status: "success" | "partial" | "failed" = "success";
  let itemsFetched = 0;
  let itemsInserted = 0;
  let itemsDeduped = 0;
  let errorMessage: string | null = null;

  try {
    if (!config.apiKey) {
      status = "partial";
      errorMessage = "NEWS_API_KEY missing - no provider fetch.";
    } else {
      const normalized = await ingestFromProvider(config.provider, config.apiKey, config.tickers);
      itemsFetched = normalized.length;
      const rows = normalized.map((r) => finalizeArticle(r, config.allowlist));

      const insertChunk = 100;
      for (let i = 0; i < rows.length; i += insertChunk) {
        const batch = rows.slice(i, i + insertChunk).map((r) => ({
          external_id: r.external_id,
          headline: r.headline,
          summary: r.summary,
          source: r.source,
          url: r.url,
          published_at: r.published_at,
          sentiment: r.sentiment,
          sentiment_model: r.sentiment_model,
          related_tickers: r.related_tickers,
          related_sectors: r.related_sectors,
          importance_score: r.importance_score,
          category: r.category,
          raw_payload: r.raw_payload,
        }));

        const { data, error } = await supabase
          .from("market_news")
          .upsert(batch, { onConflict: "url", ignoreDuplicates: true })
          .select("id");
        if (error) throw new Error(error.message);
        itemsInserted += data?.length ?? 0;
      }
      itemsDeduped = Math.max(0, rows.length - itemsInserted);
    }

    await supabase
      .from("news_ingest_runs")
      .update({
        finished_at: new Date().toISOString(),
        status,
        items_fetched: itemsFetched,
        items_inserted: itemsInserted,
        items_deduped: itemsDeduped,
        error_message: errorMessage,
      })
      .eq("id", runId);

    return {
      provider: config.provider,
      items_fetched: itemsFetched,
      items_inserted: itemsInserted,
      items_deduped: itemsDeduped,
      status,
      run_id: runId,
    };
  } catch (error) {
    status = "failed";
    errorMessage = error instanceof Error ? error.message : String(error);
    await supabase
      .from("news_ingest_runs")
      .update({
        finished_at: new Date().toISOString(),
        status,
        items_fetched: itemsFetched,
        items_inserted: itemsInserted,
        items_deduped: itemsDeduped,
        error_message: errorMessage,
      })
      .eq("id", runId);
    throw error;
  }
}
