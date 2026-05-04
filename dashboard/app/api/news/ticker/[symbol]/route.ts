import { NextRequest, NextResponse } from "next/server";

import { queryTickerNews } from "@/lib/news/queries";

interface RouteParams {
  params: Promise<{ symbol: string }>;
}

export async function GET(request: NextRequest, ctx: RouteParams) {
  try {
    const { symbol } = await ctx.params;
    const { searchParams } = new URL(request.url);
    const hours = Number(searchParams.get("hours") ?? 72);
    const limit = Number(searchParams.get("limit") ?? 20);
    const page = Number(searchParams.get("page") ?? 0);

    const items = await queryTickerNews(
      symbol,
      Number.isFinite(hours) ? hours : 72,
      Number.isFinite(page) ? page : 0,
      Number.isFinite(limit) ? limit : 20,
    );

    return NextResponse.json({ symbol: symbol.toUpperCase(), items, page });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
