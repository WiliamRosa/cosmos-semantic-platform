import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Shell } from "@/components/Shell";
import { I18nProvider } from "@/lib/i18n";
import { SessionProvider } from "@/lib/session";
import { THEME_SCRIPT, ThemeProvider } from "@/lib/theme";
import "./globals.css";

// Self-hosted at build time by next/font: no request to Google when the console runs.
const inter = Inter({ subsets: ["latin"], display: "swap", variable: "--font-inter" });

export const metadata: Metadata = {
  title: "Cosmos Semantic Console",
  description: "Health, process graphs, catalog and audited answers of a Cosmos Semantic model.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // The theme script changes the class on <html> before React hydrates.
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="antialiased">
        <ThemeProvider>
          <I18nProvider>
            <SessionProvider>
              <Shell>{children}</Shell>
            </SessionProvider>
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
