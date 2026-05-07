import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({
  revalidateTag: vi.fn(),
}));

import { GET as ingestGET, POST as ingestPOST } from "@/app/api/news/ingest/route";
import { GET as marketGET } from "@/app/api/news/market/route";
import { GET as tickerGET } from "@/app/api/news/ticker/[symbol]/route";
import { POST as revalidatePOST } from "@/app/api/technical/revalidate/route";
import * as queryModule from "@/lib/news/queries";

describe("API auth/error contracts", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.CRON_SECRET;
    delete process.env.ROUTE_AUTH_TOKENS_JSON;
  });

  it("returns structured unauthorized response for technical revalidate", async () => {
    process.env.CRON_SECRET = "secret";
    const req = new NextRequest("http://localhost/api/technical/revalidate", {
      method: "POST",
    });

    const res = await revalidatePOST(req);
    const body = await res.json();

    expect(res.status).toBe(401);
    expect(body.error?.code).toBe("UNAUTHORIZED");
    expect(typeof body.error?.request_id).toBe("string");
    expect(res.headers.get("x-request-id")).toBeTruthy();
  });

  it("returns structured internal error when market query throws", async () => {
    vi.spyOn(queryModule, "queryMarketNews").mockRejectedValue(new Error("db unavailable"));
    const req = new NextRequest("http://localhost/api/news/market?hours=72&limit=10&page=0", {
      method: "GET",
    });

    const res = await marketGET(req);
    const body = await res.json();

    expect(res.status).toBe(500);
    expect(body.error?.code).toBe("INTERNAL_ERROR");
    expect(typeof body.error?.request_id).toBe("string");
    expect(res.headers.get("x-duration-ms")).toBeTruthy();
  });

  it("returns unauthorized for ingest GET and POST without valid secret", async () => {
    process.env.CRON_SECRET = "secret";
    const getReq = new NextRequest("http://localhost/api/news/ingest", { method: "GET" });
    const postReq = new NextRequest("http://localhost/api/news/ingest", { method: "POST" });

    const [getRes, postRes] = await Promise.all([ingestGET(getReq), ingestPOST(postReq)]);
    const getBody = await getRes.json();
    const postBody = await postRes.json();

    expect(getRes.status).toBe(401);
    expect(postRes.status).toBe(401);
    expect(getBody.error?.code).toBe("UNAUTHORIZED");
    expect(postBody.error?.code).toBe("UNAUTHORIZED");
    expect(typeof getBody.error?.request_id).toBe("string");
    expect(typeof postBody.error?.request_id).toBe("string");
  });

  it("accepts scoped token for ingest and rejects wrong scope token", async () => {
    process.env.ROUTE_AUTH_TOKENS_JSON = JSON.stringify([
      { token: "token-news", scopes: ["news:ingest"], principal: "news-bot" },
      { token: "token-tech", scopes: ["technical:revalidate"], principal: "tech-bot" },
    ]);

    const okReq = new NextRequest("http://localhost/api/news/ingest", {
      method: "POST",
      headers: { authorization: "Bearer token-news" },
    });
    const badReq = new NextRequest("http://localhost/api/news/ingest", {
      method: "POST",
      headers: { authorization: "Bearer token-tech" },
    });

    const [okRes, badRes] = await Promise.all([ingestPOST(okReq), ingestPOST(badReq)]);
    const badBody = await badRes.json();

    expect(okRes.status).not.toBe(401);
    expect(badRes.status).toBe(401);
    expect(badBody.error?.code).toBe("UNAUTHORIZED");
  });

  it("accepts scoped token for technical revalidate", async () => {
    process.env.ROUTE_AUTH_TOKENS_JSON = JSON.stringify([
      { token: "token-tech", scopes: ["technical:revalidate"], principal: "tech-bot" },
    ]);
    const req = new NextRequest("http://localhost/api/technical/revalidate", {
      method: "POST",
      headers: { authorization: "Bearer token-tech" },
    });

    const res = await revalidatePOST(req);
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.ok).toBe(true);
    expect(res.headers.get("x-request-id")).toBeTruthy();
  });

  it("returns bad request for invalid ticker symbols", async () => {
    const req = new NextRequest("http://localhost/api/news/ticker/@@@", { method: "GET" });
    const res = await tickerGET(req, { params: Promise.resolve({ symbol: "@@@" }) });
    const body = await res.json();

    expect(res.status).toBe(400);
    expect(body.error?.code).toBe("BAD_REQUEST");
    expect(typeof body.error?.request_id).toBe("string");
  });

  it("normalizes and clamps ticker query parameters", async () => {
    const spy = vi
      .spyOn(queryModule, "queryTickerNews")
      .mockResolvedValue([]);
    const req = new NextRequest(
      "http://localhost/api/news/ticker/msft?hours=999&limit=999&page=-3",
      { method: "GET" },
    );

    const res = await tickerGET(req, { params: Promise.resolve({ symbol: "msft" }) });
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.symbol).toBe("MSFT");
    expect(body.page).toBe(0);
    expect(spy).toHaveBeenCalledWith("MSFT", 168, 0, 100);
    expect(res.headers.get("x-request-id")).toBeTruthy();
  });
});
