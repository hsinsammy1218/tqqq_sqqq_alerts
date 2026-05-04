export default function PerformancePlaceholderPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Performance</h1>
      <p className="text-[var(--muted)]">
        Wire this route to your existing analytics using <code className="rounded bg-zinc-900 px-1">dashboard_runs</code>
        , <code className="rounded bg-zinc-900 px-1">signal_forward_outcomes</code>, etc. News stays separate — use it as
        context when reviewing forward outcomes, not as an execution input.
      </p>
    </div>
  );
}
