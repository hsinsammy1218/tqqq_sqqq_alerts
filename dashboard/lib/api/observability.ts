import { NextRequest, NextResponse } from "next/server";

export interface RequestContext {
  requestId: string;
  startedAtMs: number;
}

export function beginRequest(request: NextRequest): RequestContext {
  const requestId =
    request.headers.get("x-request-id") ??
    (typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`);
  const startedAtMs = Date.now();
  return { requestId, startedAtMs };
}

export function withRequestHeaders(
  response: NextResponse,
  ctx: RequestContext,
  status: "ok" | "error",
): NextResponse {
  const durationMs = Date.now() - ctx.startedAtMs;
  response.headers.set("x-request-id", ctx.requestId);
  response.headers.set("x-duration-ms", String(durationMs));
  response.headers.set("x-status", status);
  return response;
}

export function logRequest(
  route: string,
  ctx: RequestContext,
  status: "ok" | "error",
  extra?: Record<string, unknown>,
): void {
  const payload = {
    route,
    request_id: ctx.requestId,
    duration_ms: Date.now() - ctx.startedAtMs,
    status,
    ...extra,
  };
  if (status === "error") {
    console.error(JSON.stringify(payload));
  } else {
    console.info(JSON.stringify(payload));
  }
}
