import Link from "next/link";

import type { DashboardRunMeta, StockSnapshotRow } from "@/lib/technical/types";
import { regimeClass } from "@/lib/ui/regimeClass";

export function TechnicalBreakdownPanel(props: {
  symbol: string;
  run: DashboardRunMeta | null;
  snapshot: StockSnapshotRow | null;
  configError: string | null;
}) {
  const { symbol, run, snapshot, configError } = props;

  if (configError) {
    return (
      <section className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        <h2 className="text-lg font-semibold">Technical snapshot</h2>
        <p className="mt-2 text-sm text-amber-200">
          Could not load technical data. Configure Supabase ({configError})
        </p>
      </section>
    );
  }

  if (!run || !snapshot) {
    return (
      <section className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        <h2 className="text-lg font-semibold">Technical snapshot</h2>
        <p className="mt-2 text-sm text-[var(--muted)]">
          No stored snapshot for <strong>{symbol}</strong> in the latest successful batch. Run{" "}
          <code className="rounded bg-zinc-900 px-1">python publish_technical_dashboard.py</code> with{" "}
          <code className="rounded bg-zinc-900 px-1">TECHNICAL_UNIVERSE</code> including this symbol, or view the{" "}
          <Link href="/leaderboard" className="underline">
            leaderboard
          </Link>
          .
        </p>
      </section>
    );
  }

  const b = snapshot.breakdown_json ?? {};
  const ind = b.indicator_snapshot ?? {};
  const bullR = b.bull_reasons ?? [];
  const bearR = b.bear_reasons ?? [];

  return (
    <section className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">Technical snapshot</h2>
        <p className="text-xs text-[var(--muted)]">
          Batch {run.started_at}
          {run.scorer_version ? (
            <>
              {" "}
              · <code className="rounded bg-zinc-900 px-1">{run.scorer_version}</code>
            </>
          ) : null}
        </p>
      </div>
      <p className="mt-1 text-xs text-[var(--muted)]">
        Checklist-derived scores (same engine as the Python bot). News below does not affect these numbers.
      </p>

      <div className="mt-4 flex flex-wrap gap-3">
        <Metric label="Confidence (dominance)" value={`${snapshot.confidence}`} />
        <Metric label="Bull strength" value={`${snapshot.bull_strength}`} />
        <Metric label="Bear strength" value={`${snapshot.bear_strength}`} />
        <div className="rounded border border-zinc-800 bg-zinc-950 px-3 py-2">
          <p className="text-xs text-[var(--muted)]">Regime</p>
          <p className={`text-sm font-medium capitalize ${regimeClass(snapshot.regime)}`}>
            {snapshot.regime.replace("_", " ")}
          </p>
        </div>
      </div>

      <dl className="mt-4 grid gap-1 text-xs text-[var(--muted)] sm:grid-cols-2">
        <div>
          <dt className="inline text-zinc-500">Daily bar end:</dt>{" "}
          <dd className="inline font-mono text-zinc-300">{snapshot.daily_bar_end ?? "—"}</dd>
        </div>
        <div>
          <dt className="inline text-zinc-500">4h bar end:</dt>{" "}
          <dd className="inline font-mono text-zinc-300">{snapshot.h4_bar_end ?? "—"}</dd>
        </div>
      </dl>

      {(ind.daily_close !== undefined || ind.h4_close !== undefined) && (
        <div className="mt-4 space-y-2 text-sm">
          <h3 className="font-medium text-[var(--text)]">Indicators (last bar)</h3>
          <div className="grid gap-1 font-mono text-xs text-zinc-400 sm:grid-cols-2">
            {ind.daily_close !== undefined ? <div>daily close: {ind.daily_close.toFixed(2)}</div> : null}
            {ind.daily_ema20 !== undefined ? <div>daily EMA20: {ind.daily_ema20.toFixed(2)}</div> : null}
            {ind.daily_ema50 !== undefined ? <div>daily EMA50: {ind.daily_ema50.toFixed(2)}</div> : null}
            {ind.daily_rsi14 !== undefined ? <div>daily RSI14: {ind.daily_rsi14.toFixed(1)}</div> : null}
            {ind.h4_close !== undefined ? <div>4h close: {ind.h4_close.toFixed(2)}</div> : null}
            {ind.h4_ema20 !== undefined ? <div>4h EMA20: {ind.h4_ema20.toFixed(2)}</div> : null}
            {ind.h4_ema50 !== undefined ? <div>4h EMA50: {ind.h4_ema50.toFixed(2)}</div> : null}
          </div>
          {b.weighted_bull !== undefined && b.weighted_bear !== undefined && b.max_weight_total !== undefined ? (
            <p className="text-xs text-[var(--muted)]">
              Weighted raw {b.weighted_bull.toFixed(2)} / {b.weighted_bear.toFixed(2)} (max sum{" "}
              {b.max_weight_total.toFixed(2)})
            </p>
          ) : null}
        </div>
      )}

      <details className="mt-4 rounded border border-zinc-800 bg-zinc-950 p-3">
        <summary className="cursor-pointer text-sm font-medium text-[var(--text)]">Weighted checklist</summary>
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <div>
            <p className="text-xs font-semibold text-emerald-500">Bull checks</p>
            <ul className="mt-1 list-inside list-disc text-xs text-zinc-400">
              {bullR.length ? bullR.map((r) => <li key={r}>{r}</li>) : <li>(none)</li>}
            </ul>
          </div>
          <div>
            <p className="text-xs font-semibold text-rose-500">Bear checks</p>
            <ul className="mt-1 list-inside list-disc text-xs text-zinc-400">
              {bearR.length ? bearR.map((r) => <li key={r}>{r}</li>) : <li>(none)</li>}
            </ul>
          </div>
        </div>
      </details>
    </section>
  );
}

function Metric(props: { label: string; value: string }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 px-3 py-2">
      <p className="text-xs text-[var(--muted)]">{props.label}</p>
      <p className="text-lg font-semibold text-[var(--text)]">{props.value}</p>
    </div>
  );
}
