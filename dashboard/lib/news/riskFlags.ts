/**
 * Research-only narrative risk / catalyst hints from recent news.
 * Does not modify technical signals.
 */
export interface NewsContextFlags {
  narrativeRisk: boolean;
  catalystSoon: boolean;
  summary: string;
}

const RISK_RE =
  /\b(downgrade|investigation|SEC|subpoena|lawsuit|miss|warning|bankruptcy|probe)\b/i;
const CATALYST_RE =
  /\b(earnings|guidance|FDA|approval|split|merger|acquisition|vote)\b/i;

export function computeNewsContextFlags(
  items: { sentiment: number | null; headline: string; summary: string | null }[],
): NewsContextFlags {
  let narrativeRisk = false;
  let catalystSoon = false;
  for (const it of items) {
    const blob = `${it.headline} ${it.summary ?? ""}`;
    if (it.sentiment != null && it.sentiment <= -0.35) narrativeRisk = true;
    if (RISK_RE.test(blob)) narrativeRisk = true;
    if (CATALYST_RE.test(blob)) catalystSoon = true;
  }
  const parts: string[] = [];
  if (narrativeRisk) parts.push("Headlines suggest narrative risk (verify vs technicals).");
  if (catalystSoon) parts.push("Possible catalyst window in recent headlines.");
  if (parts.length === 0) parts.push("No strong headline-based flags in this window.");
  return {
    narrativeRisk,
    catalystSoon,
    summary: parts.join(" "),
  };
}
