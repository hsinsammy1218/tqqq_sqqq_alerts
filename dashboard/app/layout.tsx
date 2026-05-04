import type { Metadata } from "next";

import { SiteNav } from "@/components/SiteNav";

import "./globals.css";

export const metadata: Metadata = {
  title: "Stock Signal Dashboard",
  description: "Research dashboard — technical signals plus market news context (no execution).",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SiteNav />
        <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
        <footer className="mx-auto max-w-5xl px-4 pb-8 text-center text-xs text-[var(--muted)]">
          Alert-only research context. News does not override technical scores. No broker APIs.
        </footer>
      </body>
    </html>
  );
}
