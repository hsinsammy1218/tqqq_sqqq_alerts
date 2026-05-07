import { revalidateTag } from "next/cache";
import { NextRequest, NextResponse } from "next/server";
import { errorResponse } from "@/lib/api/errorResponse";
import {
  beginRequest,
  logRequest,
  withRequestHeaders,
} from "@/lib/api/observability";
import { authorizeRouteScope } from "@/lib/api/routeAuth";

/** Called after Python `publish_technical_dashboard.py` finishes (optional). */
export async function POST(request: NextRequest) {
  const ctx = beginRequest(request);
  const auth = authorizeRouteScope(request, "technical:revalidate");
  if (!auth.ok) {
    logRequest("/api/technical/revalidate", ctx, "error", {
      reason: "unauthorized",
      auth_reason: auth.reason,
    });
    return withRequestHeaders(
      errorResponse("Unauthorized", 401, "UNAUTHORIZED", ctx.requestId),
      ctx,
      "error",
    );
  }
  revalidateTag("technical");
  logRequest("/api/technical/revalidate", ctx, "ok", {
    revalidated: "technical",
    auth_mode: auth.mode,
    principal: auth.principal,
  });
  return withRequestHeaders(
    NextResponse.json({ ok: true, revalidated: "technical", request_id: ctx.requestId }),
    ctx,
    "ok",
  );
}
