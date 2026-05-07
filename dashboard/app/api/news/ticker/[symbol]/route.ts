import { NextRequest, NextResponse } from "next/server";

import { errorResponse } from "@/lib/api/errorResponse";
import {
  beginRequest,
  logRequest,
  withRequestHeaders,
} from "@/lib/api/observability";
import { queryTickerNews } from "@/lib/news/queries";

interface RouteParams {
  params: Promise<{ symbol: string }>;
}

export async function GET(request: NextRequest, ctx: RouteParams) {
  const req = beginRequest(request);
  try {
    const { symbol } = await ctx.params;
    const normalized = symbol.trim().toUpperCase();
    if (!/^[A-Z0-9.\-]{1,10}$/.test(normalized)) {
      logRequest("/api/news/ticker", req, "error", { reason: "invalid_symbol", symbol });
      return withRequestHeaders(
        errorResponse("Invalid symbol", 400, "BAD_REQUEST", req.requestId),
        req,
        "error",
      );
    }
    const { searchParams } = new URL(request.url);
    const hours = Number(searchParams.get("hours") ?? 72);
    const limit = Number(searchParams.get("limit") ?? 20);
    const page = Number(searchParams.get("page") ?? 0);
    const parsedHours = Number.isFinite(hours) ? Math.min(168, Math.max(6, hours)) : 72;
    const parsedLimit = Number.isFinite(limit) ? Math.min(100, Math.max(1, limit)) : 20;
    const parsedPage = Number.isFinite(page) ? Math.max(0, page) : 0;

    const items = await queryTickerNews(
      normalized,
      parsedHours,
      parsedPage,
      parsedLimit,
    );

    const response = NextResponse.json({
      symbol: normalized,
      items,
      page: parsedPage,
      request_id: req.requestId,
    });
    logRequest("/api/news/ticker", req, "ok", { symbol: normalized, page: parsedPage });
    return withRequestHeaders(response, req, "ok");
  } catch (e) {
    logRequest("/api/news/ticker", req, "error", { error: String(e) });
    return withRequestHeaders(
      errorResponse("Internal server error", 500, "INTERNAL_ERROR", req.requestId),
      req,
      "error",
    );
  }
}
