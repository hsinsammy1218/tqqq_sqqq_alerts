export function regimeClass(regime: string): string {
  if (regime === "trend_up") return "text-emerald-400";
  if (regime === "trend_down") return "text-rose-400";
  return "text-zinc-400";
}
