import { unstable_cache } from "next/cache";

import { createServiceClient } from "@/lib/supabase/service";

import type { DashboardRunMeta, StockSnapshotRow } from "./types";

async function latestOkRunInternal(): Promise<DashboardRunMeta | null> {
  const supabase = createServiceClient();
  const { data, error } = await supabase
    .from("dashboard_runs")
    .select("id, started_at, finished_at, status, scorer_version, tickers_requested, tickers_succeeded")
    .eq("status", "ok")
    .order("started_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (error) throw new Error(error.message);
  if (!data) return null;
  return data as DashboardRunMeta;
}

async function leaderboardForRunInternal(runId: string): Promise<StockSnapshotRow[]> {
  const supabase = createServiceClient();
  const { data, error } = await supabase
    .from("stock_snapshots")
    .select(
      "id, dashboard_run_id, symbol, computed_at, daily_bar_end, h4_bar_end, regime, bull_strength, bear_strength, confidence, breakdown_json",
    )
    .eq("dashboard_run_id", runId)
    .order("confidence", { ascending: false })
    .order("bull_strength", { ascending: false })
    .order("symbol", { ascending: true });
  if (error) throw new Error(error.message);
  return (data ?? []) as StockSnapshotRow[];
}

async function snapshotForTickerInternal(runId: string, symbol: string): Promise<StockSnapshotRow | null> {
  const supabase = createServiceClient();
  const sym = symbol.trim().toUpperCase();
  const { data, error } = await supabase
    .from("stock_snapshots")
    .select(
      "id, dashboard_run_id, symbol, computed_at, daily_bar_end, h4_bar_end, regime, bull_strength, bear_strength, confidence, breakdown_json",
    )
    .eq("dashboard_run_id", runId)
    .eq("symbol", sym)
    .maybeSingle();
  if (error) throw new Error(error.message);
  return (data as StockSnapshotRow) ?? null;
}

/** Latest successful batch metadata (cached). */
export function getLatestOkRunCached() {
  return unstable_cache(async () => latestOkRunInternal(), ["technical-latest-run"], {
    tags: ["technical"],
    revalidate: 120,
  })();
}

/** Full leaderboard for latest OK run (cached). */
export function getLeaderboardSnapshotsCached() {
  return unstable_cache(
    async () => {
      const run = await latestOkRunInternal();
      if (!run) return { run: null as DashboardRunMeta | null, snapshots: [] as StockSnapshotRow[] };
      const snapshots = await leaderboardForRunInternal(run.id);
      return { run, snapshots };
    },
    ["technical-leaderboard"],
    { tags: ["technical"], revalidate: 120 },
  )();
}

export function getTickerSnapshotCached(symbol: string) {
  const sym = symbol.trim().toUpperCase();
  return unstable_cache(
    async () => {
      const run = await latestOkRunInternal();
      if (!run) return { run: null as DashboardRunMeta | null, snapshot: null as StockSnapshotRow | null };
      const snapshot = await snapshotForTickerInternal(run.id, sym);
      return { run, snapshot };
    },
    ["technical-ticker", sym],
    { tags: ["technical"], revalidate: 120 },
  )();
}
