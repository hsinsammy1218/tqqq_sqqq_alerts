import { NextRequest, NextResponse } from "next/server";

import { queryMarketNews } from "@/lib/news/queries";

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);
    const hours = Number(searchParams.get("hours") ?? 72);
    const limit = Number(searchParams.get("limit") ?? 20);
    const page = Number(searchParams.get("page") ?? 0);
    const sector = searchParams.get("sector");

    const lim = Number.isFinite(limit) ? limit : 20;
    const pg = Number.isFinite(page) ? page : 0;

    const items = await queryMarketNews({
      hours: Number.isFinite(hours) ? hours : 72,
      sector: sector?.trim() || null,
      page: pg,
      limit: lim,
    });

    const hasMore = items.length === lim;

    return NextResponse.json({
      items,
      page: pg,
      has_more: hasMore,
      next_page: hasMore ? pg + 1 : null,
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
