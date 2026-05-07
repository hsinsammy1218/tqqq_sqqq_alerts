import { NextRequest } from "next/server";

type RouteScope = "news:ingest" | "technical:revalidate";

interface ScopedTokenConfig {
  token: string;
  scopes: RouteScope[];
  principal?: string;
}

export interface AuthResult {
  ok: boolean;
  mode: "cron_secret" | "scoped_token" | "none";
  principal?: string;
  reason?: string;
}

function readPresentedToken(request: NextRequest): string | null {
  const auth = request.headers.get("authorization");
  const bearer = auth?.startsWith("Bearer ") ? auth.slice(7).trim() : null;
  const headerSecret = request.headers.get("x-cron-secret")?.trim() || null;
  return bearer || headerSecret || null;
}

function parseScopedTokens(raw: string | undefined): ScopedTokenConfig[] {
  if (!raw?.trim()) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const valid: ScopedTokenConfig[] = [];
  for (const item of parsed) {
    if (!item || typeof item !== "object") continue;
    const token = (item as { token?: unknown }).token;
    const scopes = (item as { scopes?: unknown }).scopes;
    const principal = (item as { principal?: unknown }).principal;
    if (typeof token !== "string" || !token.trim()) continue;
    if (!Array.isArray(scopes)) continue;
    const normalizedScopes = scopes.filter(
      (s): s is RouteScope =>
        typeof s === "string" &&
        (s === "news:ingest" || s === "technical:revalidate"),
    );
    if (normalizedScopes.length === 0) continue;
    valid.push({
      token: token.trim(),
      scopes: normalizedScopes,
      principal: typeof principal === "string" && principal.trim()
        ? principal.trim()
        : undefined,
    });
  }
  return valid;
}

export function authorizeRouteScope(
  request: NextRequest,
  requiredScope: RouteScope,
): AuthResult {
  const presented = readPresentedToken(request);
  if (!presented) {
    return { ok: false, mode: "none", reason: "missing_token" };
  }

  const legacySecret = process.env.CRON_SECRET?.trim();
  if (legacySecret && presented === legacySecret) {
    return { ok: true, mode: "cron_secret", principal: "cron_secret" };
  }

  const scoped = parseScopedTokens(process.env.ROUTE_AUTH_TOKENS_JSON);
  const match = scoped.find((cfg) => cfg.token === presented);
  if (!match) {
    return { ok: false, mode: "none", reason: "invalid_token" };
  }
  if (!match.scopes.includes(requiredScope)) {
    return {
      ok: false,
      mode: "scoped_token",
      principal: match.principal ?? "scoped_token",
      reason: "insufficient_scope",
    };
  }

  return {
    ok: true,
    mode: "scoped_token",
    principal: match.principal ?? "scoped_token",
  };
}
