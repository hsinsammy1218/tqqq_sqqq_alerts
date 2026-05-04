import Link from "next/link";

export function SiteNav() {
  return (
    <header className="border-b border-zinc-800 bg-zinc-950">
      <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-4 px-4 py-3">
        <Link href="/" className="font-semibold text-[var(--text)]">
          Stock Signal Dashboard
        </Link>
        <nav className="flex flex-wrap gap-3 text-sm">
          <Link href="/">Home</Link>
          <Link href="/market-news">Market news</Link>
          <Link href="/stock/QQQ">Sample: QQQ</Link>
          <Link href="/performance">Performance</Link>
        </nav>
      </div>
    </header>
  );
}
