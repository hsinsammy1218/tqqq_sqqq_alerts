/** Strip common tracking params for stable dedupe keys. */
export function normalizeNewsUrl(raw: string): string {
  try {
    const u = new URL(raw);
    const drop = [
      "utm_source",
      "utm_medium",
      "utm_campaign",
      "utm_term",
      "utm_content",
      "ref",
      "src",
    ];
    drop.forEach((k) => u.searchParams.delete(k));
    u.hash = "";
    return u.toString();
  } catch {
    return raw.trim();
  }
}
