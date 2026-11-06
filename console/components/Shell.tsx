"use client";

import {
  Activity,
  BookOpen,
  CircleDollarSign,
  KeyRound,
  LayoutDashboard,
  Languages,
  MessageSquareText,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { DEMO } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";

const NAV: { href: string; key: Key; icon: typeof Activity }[] = [
  { href: "/", key: "nav.overview", icon: LayoutDashboard },
  { href: "/processes/", key: "nav.processes", icon: Workflow },
  { href: "/catalog/", key: "nav.catalog", icon: BookOpen },
  { href: "/answers/", key: "nav.answers", icon: MessageSquareText },
  { href: "/health/", key: "nav.health", icon: Activity },
  { href: "/usage/", key: "nav.usage", icon: CircleDollarSign },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { t, lang, setLang } = useI18n();
  const { needsAuth, hasCredential, requireAuth, signOut } = useSession();
  const path = usePathname();
  const active = (href: string) =>
    href === "/" ? path === "/" || path === "" : path.startsWith(href.replace(/\/$/, ""));

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3">
          <Link href="/" className="flex items-center gap-2">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-slate-900 text-sm font-bold text-white dark:bg-white dark:text-slate-900">
              C
            </span>
            <span className="font-semibold">{t("app.title")}</span>
            <span className="hidden text-slate-500 sm:inline">{t("app.subtitle")}</span>
          </Link>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setLang(lang === "en" ? "pt" : "en")}
              className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              aria-label={t("lang.switch")}
            >
              <Languages className="h-4 w-4" />
              {t("lang.switch")}
            </button>
            {!DEMO && (
            <button
              onClick={hasCredential ? signOut : requireAuth}
              className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              <KeyRound className="h-4 w-4" />
              {hasCredential ? t("auth.signout") : t("auth.change")}
            </button>
            )}
          </div>
        </div>
        <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4 pb-2">
          {NAV.map(({ href, key, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm ${
                active(href)
                  ? "bg-slate-900 text-white dark:bg-white dark:text-slate-900"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
              }`}
            >
              <Icon className="h-4 w-4" />
              {t(key)}
            </Link>
          ))}
        </nav>
      </header>
      {DEMO && (
        <div className="border-b border-sky-200 bg-sky-50 text-sm text-sky-900 dark:border-sky-900 dark:bg-sky-950 dark:text-sky-100">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2">
            <span className="font-medium">{t("demo.banner")}</span>
            <code className="rounded bg-white/70 px-1.5 py-0.5 font-mono text-xs dark:bg-slate-900/60">
              pip install "cosmos-semantic[duckdb,testdata,server]"
            </code>
            <a
              href="https://github.com/WiliamRosa/cosmos-semantic-platform"
              className="font-medium underline underline-offset-2"
            >
              GitHub →
            </a>
          </div>
        </div>
      )}
      <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      {needsAuth && <SignIn />}
    </div>
  );
}

function SignIn() {
  const { t } = useI18n();
  const { signIn, dismissAuth } = useSession();
  const [value, setValue] = useState("");
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && dismissAuth();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dismissAuth]);
  return (
    <div
      className="fixed inset-0 z-30 grid place-items-center bg-slate-900/40 p-4"
      onClick={(e) => e.target === e.currentTarget && dismissAuth()}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby="sign-in-title"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) signIn(value);
        }}
        className="w-full max-w-md rounded-xl bg-white p-6 shadow-xl dark:bg-slate-900"
      >
        <h2 id="sign-in-title" className="text-lg font-semibold">{t("auth.title")}</h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("auth.help")}</p>
        <input
          autoFocus
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={t("auth.placeholder")}
          className="mt-4 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-200 dark:border-slate-700 dark:bg-slate-800"
        />
        <div className="mt-4 flex gap-2">
          <button
            type="button"
            onClick={dismissAuth}
            className="flex-1 rounded-lg px-3 py-2 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50 dark:text-slate-300 dark:ring-slate-700 dark:hover:bg-slate-800"
          >
            {t("auth.cancel")}
          </button>
          <button
            type="submit"
            className="flex-1 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-white dark:text-slate-900"
            disabled={!value.trim()}
          >
            {t("auth.save")}
          </button>
        </div>
      </form>
    </div>
  );
}
