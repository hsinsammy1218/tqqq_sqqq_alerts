import { NextRequest, NextResponse } from "next/server";

import { errorResponse } from "@/lib/api/errorResponse";
import {
  beginRequest,
  logRequest,
  withRequestHeaders,
} from "@/lib/api/observability";
import { queryMarketNews } from "@/lib/news/queries";

export async function GET(request: NextRequest) {
  const ctx = beginRequest(request);
  try {
    const { searchParams } = new URL(request.url);
    const hours = Number(searchParams.get("hours") ?? 72);
    const limit = Number(searchParams.get("limit") ?? 20);
    const page = Number(searchParams.get("page") ?? 0);
    const sector = searchParams.get("sector");

    const lim = Number.isFinite(limit) ? Math.min(100, Math.max(1, limit)) : 20;
    const pg = Number.isFinite(page) ? Math.max(0, page) : 0;
    const parsedHours = Number.isFinite(hours) ? Math.min(168, Math.max(6, hours)) : 72;

    const items = await queryMarketNews({
      hours: parsedHours,
      sector: sector?.trim() || null,
      page: pg,
      limit: lim,
    });

    const hasMore = items.length === lim;

    const response = NextResponse.json({
      items,
      page: pg,
      has_more: hasMore,
      next_page: hasMore ? pg + 1 : null,
      request_id: ctx.requestId,
    });
    logRequest("/api/news/market", ctx, "ok", { page: pg, limit: lim });
    return withRequestHeaders(response, ctx, "ok");
  } catch (e) {
    logRequest("/api/news/market", ctx, "error", { error: String(e) });
    return withRequestHeaders(
      errorResponse("Internal server error", 500, "INTERNAL_ERROR", ctx.requestId),
      ctx,
      "error",
    );
  }
}
