import type { Metadata } from "next";
import { Shell } from "@/components/Shell";
import { I18nProvider } from "@/lib/i18n";
import { SessionProvider } from "@/lib/session";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cosmos Semantic Console",
  description: "Health, process graphs, catalog and audited answers of a Cosmos Semantic model.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">
        <I18nProvider>
          <SessionProvider>
            <Shell>{children}</Shell>
          </SessionProvider>
        </I18nProvider>
      </body>
    </html>
  );
}
