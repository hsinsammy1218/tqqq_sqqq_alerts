import { NextResponse } from "next/server";

import { createServiceClient } from "@/lib/supabase/service";

export async function GET() {
  try {
    const supabase = createServiceClient();
    const { error } = await supabase
      .from("news_ingest_runs")
      .select("id")
      .limit(1);

    if (error) {
      return NextResponse.json(
        { ok: false, status: "degraded", checks: { supabase: "error" } },
        { status: 503 },
      );
    }

    return NextResponse.json({
      ok: true,
      status: "healthy",
      checks: { supabase: "ok" },
    });
  } catch {
    return NextResponse.json(
      { ok: false, status: "degraded", checks: { supabase: "unavailable" } },
      { status: 503 },
    );
  }
}
