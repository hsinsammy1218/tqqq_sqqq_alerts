import { revalidateTag } from "next/cache";
import { NextRequest, NextResponse } from "next/server";

import { errorResponse } from "@/lib/api/errorResponse";
import {
  beginRequest,
  logRequest,
  withRequestHeaders,
} from "@/lib/api/observability";
import { authorizeRouteScope } from "@/lib/api/routeAuth";
import { runNewsIngest } from "@/lib/news/ingestService";
import { createServiceClient } from "@/lib/supabase/service";

export const maxDuration = 120;

async function runIngest(_request: NextRequest) {
  const ctx = beginRequest(_request);
  const provider = (process.env.NEWS_PROVIDER ?? "finnhub").toLowerCase();
  const apiKey = process.env.NEWS_API_KEY ?? "";
  const tickerEnv = process.env.NEWS_INGEST_TICKERS ?? "QQQ,SPY,AAPL,MSFT,NVDA";
  const tickers = tickerEnv.split(",").map((s) => s.trim()).filter(Boolean);

  let supabase;
  try {
    supabase = createServiceClient();
  } catch (e) {
    logRequest("/api/news/ingest", ctx, "error", {
      reason: "supabase_config",
      error: String(e),
    });
    return withRequestHeaders(
      errorResponse("Supabase not configured", 500, "INTERNAL_ERROR", ctx.requestId),
      ctx,
      "error",
    );
  }

  try {
    const summary = await runNewsIngest(supabase, {
      provider,
      apiKey,
      tickers,
      allowlist: process.env.NEWS_TICKER_ALLOWLIST,
    });

    revalidateTag("news");

    logRequest("/api/news/ingest", ctx, "ok", {
      run_id: summary.run_id,
      items_fetched: summary.items_fetched,
      items_inserted: summary.items_inserted,
      items_deduped: summary.items_deduped,
      ingest_status: summary.status,
    });

    return withRequestHeaders(
      NextResponse.json({
      ok: true,
      ...summary,
      request_id: ctx.requestId,
      }),
      ctx,
      "ok",
    );
  } catch (e) {
    logRequest("/api/news/ingest", ctx, "error", { error: String(e) });
    return withRequestHeaders(
      errorResponse("Ingest failed", 500, "INTERNAL_ERROR", ctx.requestId),
      ctx,
      "error",
    );
  }
}

/** Vercel Cron issues GET with Bearer CRON_SECRET. Manual runs may use POST. */
export async function GET(request: NextRequest) {
  const auth = authorizeRouteScope(request, "news:ingest");
  if (!auth.ok) {
    const ctx = beginRequest(request);
    logRequest("/api/news/ingest", ctx, "error", {
      reason: "unauthorized",
      auth_reason: auth.reason,
    });
    return withRequestHeaders(
      errorResponse("Unauthorized", 401, "UNAUTHORIZED", ctx.requestId),
      ctx,
      "error",
    );
  }
  const ctx = beginRequest(request);
  logRequest("/api/news/ingest", ctx, "ok", {
    auth_mode: auth.mode,
    principal: auth.principal,
  });
  return runIngest(request);
}

export async function POST(request: NextRequest) {
  const auth = authorizeRouteScope(request, "news:ingest");
  if (!auth.ok) {
    const ctx = beginRequest(request);
    logRequest("/api/news/ingest", ctx, "error", {
      reason: "unauthorized",
      auth_reason: auth.reason,
    });
    return withRequestHeaders(
      errorResponse("Unauthorized", 401, "UNAUTHORIZED", ctx.requestId),
      ctx,
      "error",
    );
  }
  const ctx = beginRequest(request);
  logRequest("/api/news/ingest", ctx, "ok", {
    auth_mode: auth.mode,
    principal: auth.principal,
  });
  return runIngest(request);
}
