import { revalidateTag } from "next/cache";
import { NextRequest, NextResponse } from "next/server";

import { finalizeArticle } from "@/lib/news/finalizeArticle";
import { ingestFromProvider } from "@/lib/news/providers";
import { createServiceClient } from "@/lib/supabase/service";

export const maxDuration = 120;

function authorizeCron(request: NextRequest): boolean {
  const secret = process.env.CRON_SECRET;
  if (!secret) return false;
  const auth = request.headers.get("authorization");
  const bearer = auth?.startsWith("Bearer ") ? auth.slice(7) : null;
  const headerSecret = request.headers.get("x-cron-secret");
  return bearer === secret || headerSecret === secret;
}

async function runIngest(_request: NextRequest) {
  const provider = (process.env.NEWS_PROVIDER ?? "finnhub").toLowerCase();
  const apiKey = process.env.NEWS_API_KEY ?? "";
  const tickerEnv = process.env.NEWS_INGEST_TICKERS ?? "QQQ,SPY,AAPL,MSFT,NVDA";
  const tickers = tickerEnv.split(",").map((s) => s.trim()).filter(Boolean);

  let supabase;
  try {
    supabase = createServiceClient();
  } catch (e) {
    return NextResponse.json(
      { error: "Supabase not configured", detail: String(e) },
      { status: 500 },
    );
  }

  const runInsert = await supabase
    .from("news_ingest_runs")
    .insert({
      status: "running",
      provider,
      items_fetched: 0,
      items_inserted: 0,
      items_deduped: 0,
    })
    .select("id")
    .single();

  const runId = runInsert.data?.id as string | undefined;
  if (runInsert.error || !runId) {
    return NextResponse.json(
      { error: "Could not start ingest run", detail: runInsert.error?.message },
      { status: 500 },
    );
  }

  let itemsFetched = 0;
  let itemsInserted = 0;
  let itemsDeduped = 0;
  let status: "success" | "partial" | "failed" = "success";
  let errorMessage: string | null = null;

  try {
    if (!apiKey) {
      status = "partial";
      errorMessage = "NEWS_API_KEY missing — no provider fetch.";
    } else {
      const normalized = await ingestFromProvider(provider, apiKey, tickers);
      itemsFetched = normalized.length;
      const allowlist = process.env.NEWS_TICKER_ALLOWLIST;
      const rows = normalized.map((r) => finalizeArticle(r, allowlist));

      const urls = rows.map((r) => r.url);
      const existing = new Set<string>();
      const chunk = 200;
      for (let i = 0; i < urls.length; i += chunk) {
        const slice = urls.slice(i, i + chunk);
        const { data } = await supabase.from("market_news").select("url").in("url", slice);
        for (const row of data ?? []) {
          existing.add(String((row as { url: string }).url));
        }
      }

      const toInsert = rows.filter((r) => !existing.has(r.url));
      itemsDeduped = rows.length - toInsert.length;

      const insertChunk = 50;
      for (let i = 0; i < toInsert.length; i += insertChunk) {
        const batch = toInsert.slice(i, i + insertChunk).map((r) => ({
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
        const ins = await supabase.from("market_news").insert(batch);
        if (ins.error) throw new Error(ins.error.message);
        itemsInserted += batch.length;
      }
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

    revalidateTag("news");

    return NextResponse.json({
      ok: true,
      provider,
      items_fetched: itemsFetched,
      items_inserted: itemsInserted,
      items_deduped: itemsDeduped,
      status,
      run_id: runId,
    });
  } catch (e) {
    status = "failed";
    errorMessage = String(e);
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

    return NextResponse.json(
      { ok: false, error: errorMessage, run_id: runId },
      { status: 500 },
    );
  }
}

/** Vercel Cron issues GET with Bearer CRON_SECRET. Manual runs may use POST. */
export async function GET(request: NextRequest) {
  if (!authorizeCron(request)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  return runIngest(request);
}

export async function POST(request: NextRequest) {
  if (!authorizeCron(request)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  return runIngest(request);
}
