"use client";

import {
  Activity,
  BookOpen,
  BookText,
  ChevronsLeft,
  ChevronsRight,
  CircleDollarSign,
  KeyRound,
  LayoutDashboard,
  Languages,
  Menu,
  MessageSquareText,
  Monitor,
  Moon,
  Search,
  Sun,
  Telescope,
  Workflow,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { DEMO } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import { type Theme, useTheme } from "@/lib/theme";
import { CommandPalette } from "./CommandPalette";
import { Logo } from "./Logo";

const NAV: { href: string; key: Key; icon: typeof Activity }[] = [
  { href: "/", key: "nav.overview", icon: LayoutDashboard },
  { href: "/processes/", key: "nav.processes", icon: Workflow },
  { href: "/catalog/", key: "nav.catalog", icon: BookOpen },
  { href: "/answers/", key: "nav.answers", icon: MessageSquareText },
  { href: "/explore/", key: "nav.explore", icon: Telescope },
  { href: "/health/", key: "nav.health", icon: Activity },
  { href: "/usage/", key: "nav.usage", icon: CircleDollarSign },
];

const DOCS = "https://github.com/WiliamRosa/cosmos-semantic-platform/blob/main/docs/console.md";
const SIDEBAR = "cosmos.sidebar";

export function Shell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  const { needsAuth } = useSession();
  const path = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [palette, setPalette] = useState(false);

  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(SIDEBAR) === "collapsed");
    } catch {
      /* expanded */
    }
  }, []);
  const toggleCollapsed = () => {
    setCollapsed((c) => {
      try {
        localStorage.setItem(SIDEBAR, c ? "expanded" : "collapsed");
      } catch {
        /* not remembered */
      }
      return !c;
    });
  };

  // The drawer closes after navigating; Ctrl+K / ⌘K opens the search from anywhere.
  useEffect(() => setDrawer(false), [path]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((p) => !p);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const pages = useMemo(() => NAV.map((n) => ({ href: n.href, title: t(n.key) })), [t]);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <a
        href="#content"
        className="sr-only z-50 rounded-lg bg-white px-3 py-2 text-sm font-medium shadow focus:not-sr-only focus:fixed focus:left-3 focus:top-3 dark:bg-slate-900"
      >
        {t("nav.skip")}
      </a>

      {/* Desktop sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-30 hidden flex-col border-r border-slate-200 bg-white transition-[width] duration-200 lg:flex dark:border-slate-800 dark:bg-slate-900 ${
          collapsed ? "w-16" : "w-60"
        }`}
      >
        <Sidebar path={path} collapsed={collapsed} onToggle={toggleCollapsed} />
      </aside>

      {/* Mobile drawer */}
      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true">
          <div className="absolute inset-0 bg-slate-900/50" onClick={() => setDrawer(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-64 flex-col border-r border-slate-200 bg-white shadow-xl dark:border-slate-800 dark:bg-slate-900">
            <button
              onClick={() => setDrawer(false)}
              aria-label={t("nav.close")}
              className="absolute right-3 top-4 grid h-8 w-8 place-items-center rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
            >
              <X className="h-4 w-4" />
            </button>
            <Sidebar path={path} collapsed={false} />
          </aside>
        </div>
      )}

      <div
        className={`flex min-h-screen flex-col transition-[padding] duration-200 ${
          collapsed ? "lg:pl-16" : "lg:pl-60"
        }`}
      >
        <TopBar onMenu={() => setDrawer(true)} onSearch={() => setPalette(true)} />
        {DEMO && <DemoBanner />}
        <main id="content" className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6 lg:px-8">
          {children}
        </main>
      </div>

      {palette && <CommandPalette pages={pages} onClose={() => setPalette(false)} />}
      {needsAuth && <SignIn />}
    </div>
  );
}

function Sidebar({
  path,
  collapsed,
  onToggle,
}: {
  path: string;
  collapsed: boolean;
  onToggle?: () => void;
}) {
  const { t } = useI18n();
  const active = (href: string) =>
    href === "/" ? path === "/" || path === "" : path.startsWith(href.replace(/\/$/, ""));
  const item = (isActive: boolean) =>
    `flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm font-medium transition-colors ${
      isActive
        ? "bg-slate-900 text-white dark:bg-white dark:text-slate-900"
        : "text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100"
    } ${collapsed ? "justify-center" : ""}`;
  return (
    <>
      <Link
        href="/"
        className={`flex h-16 shrink-0 items-center gap-2.5 ${collapsed ? "justify-center" : "px-4"}`}
      >
        <Logo />
        {!collapsed && (
          <span className="leading-tight">
            <span className="block text-sm font-semibold">{t("app.title")}</span>
            <span className="block text-xs text-slate-500">{t("app.subtitle")}</span>
          </span>
        )}
      </Link>
      <nav aria-label={t("app.subtitle")} className="flex-1 space-y-1 overflow-y-auto px-3 py-2">
        {NAV.map(({ href, key, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            aria-current={active(href) ? "page" : undefined}
            title={collapsed ? t(key) : undefined}
            className={item(active(href))}
          >
            <Icon className="h-4 w-4 shrink-0" aria-hidden />
            {collapsed ? <span className="sr-only">{t(key)}</span> : <span className="truncate">{t(key)}</span>}
          </Link>
        ))}
      </nav>
      <div className="space-y-1 border-t border-slate-200 px-3 py-3 dark:border-slate-800">
        <a
          href={DOCS}
          target="_blank"
          rel="noreferrer"
          title={collapsed ? t("nav.docs") : undefined}
          className={item(false)}
        >
          <BookText className="h-4 w-4 shrink-0" aria-hidden />
          {collapsed ? <span className="sr-only">{t("nav.docs")}</span> : <span>{t("nav.docs")}</span>}
        </a>
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            aria-label={collapsed ? t("nav.expand") : t("nav.collapse")}
            title={collapsed ? t("nav.expand") : t("nav.collapse")}
            className={`${item(false)} w-full`}
          >
            {collapsed ? <ChevronsRight className="h-4 w-4" /> : <ChevronsLeft className="h-4 w-4" />}
            {!collapsed && <span>{t("nav.collapse")}</span>}
          </button>
        )}
      </div>
    </>
  );
}

function TopBar({ onMenu, onSearch }: { onMenu: () => void; onSearch: () => void }) {
  const { t, lang, setLang } = useI18n();
  const { hasCredential, requireAuth, signOut } = useSession();
  const [mac, setMac] = useState(false);
  useEffect(() => setMac(/Mac|iPhone|iPad/.test(navigator.userAgent)), []);
  const ghost =
    "inline-flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-sm text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white";
  return (
    <header className="sticky top-0 z-20 flex h-16 items-center gap-3 border-b border-slate-200 bg-white/85 px-4 backdrop-blur sm:px-6 lg:px-8 dark:border-slate-800 dark:bg-slate-900/85">
      <button onClick={onMenu} aria-label={t("nav.menu")} className={`${ghost} lg:hidden`}>
        <Menu className="h-5 w-5" />
      </button>
      <Link href="/" className="flex items-center gap-2 lg:hidden">
        <Logo className="h-7 w-7" />
        <span className="hidden text-sm font-semibold sm:inline">{t("app.title")}</span>
      </Link>
      <button
        type="button"
        onClick={onSearch}
        className="ml-auto flex h-9 w-full max-w-[12rem] items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 text-sm text-slate-500 hover:border-slate-300 hover:bg-white sm:max-w-sm lg:ml-0 dark:border-slate-700 dark:bg-slate-800/60 dark:hover:bg-slate-800"
      >
        <Search className="h-4 w-4 shrink-0" aria-hidden />
        <span className="flex-1 truncate text-left">{t("search.open")}</span>
        <kbd className="hidden rounded border border-slate-200 bg-white px-1.5 font-sans text-[11px] text-slate-400 sm:inline dark:border-slate-600 dark:bg-slate-900">
          {mac ? "⌘K" : "Ctrl K"}
        </kbd>
      </button>
      <div className="flex shrink-0 items-center gap-1 lg:ml-auto">
        <button
          onClick={() => setLang(lang === "en" ? "pt" : "en")}
          className={ghost}
          aria-label={t("lang.switch")}
          title={t("lang.switch")}
        >
          <Languages className="h-4 w-4" />
          <span className="hidden md:inline">{t("lang.switch")}</span>
        </button>
        <ThemeButton className={ghost} />
        {!DEMO && (
          <button onClick={hasCredential ? signOut : requireAuth} className={ghost}>
            <KeyRound className="h-4 w-4" />
            <span className="hidden md:inline">
              {hasCredential ? t("auth.signout") : t("auth.change")}
            </span>
          </button>
        )}
      </div>
    </header>
  );
}

const THEMES: Theme[] = ["light", "dark", "system"];

/** Cycles light → dark → system; the icon shows the current choice. */
function ThemeButton({ className }: { className: string }) {
  const { t } = useI18n();
  const { theme, setTheme } = useTheme();
  const Icon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  const label = `${t("theme.label")}: ${t(`theme.${theme}` as Key)}`;
  return (
    <button onClick={() => setTheme(next)} className={className} aria-label={label} title={label}>
      <Icon className="h-4 w-4" />
    </button>
  );
}

function DemoBanner() {
  const { t } = useI18n();
  return (
    <div className="border-b border-sky-200 bg-sky-50 text-sm text-sky-900 dark:border-sky-900/60 dark:bg-sky-950/60 dark:text-sky-100">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 sm:px-6 lg:px-8">
        <span className="font-medium">{t("demo.banner")}</span>
        <code className="rounded bg-white/70 px-1.5 py-0.5 font-mono text-xs dark:bg-slate-900/60">
          pip install &quot;cosmos-semantic[duckdb,testdata,server]&quot;
        </code>
        <a
          href="https://github.com/WiliamRosa/cosmos-semantic-platform"
          className="font-medium underline underline-offset-2"
        >
          GitHub →
        </a>
      </div>
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
      className="fixed inset-0 z-50 grid place-items-center bg-slate-900/50 p-4 backdrop-blur-sm"
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
        className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-2xl dark:border-slate-700 dark:bg-slate-900"
      >
        <Logo />
        <h2 id="sign-in-title" className="mt-4 text-lg font-semibold">
          {t("auth.title")}
        </h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("auth.help")}</p>
        <input
          autoFocus
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={t("auth.placeholder")}
          aria-label={t("auth.placeholder")}
          className="mt-4 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 focus-visible:outline-none dark:border-slate-700 dark:bg-slate-800"
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
