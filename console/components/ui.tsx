"use client";

import Link from "next/link";
import type { Reason, State } from "@/lib/api";
import { causeKey, useI18n } from "@/lib/i18n";
import type { Loaded } from "@/lib/session";

export const STATE_STYLE: Record<State, { dot: string; badge: string; ring: string; fill: string }> = {
  healthy: {
    dot: "bg-emerald-500",
    badge: "bg-emerald-50 text-emerald-800 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-800",
    ring: "border-emerald-400",
    fill: "#10b981",
  },
  attention: {
    dot: "bg-amber-500",
    badge: "bg-amber-50 text-amber-900 ring-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-800",
    ring: "border-amber-400",
    fill: "#f59e0b",
  },
  failing: {
    dot: "bg-rose-500",
    badge: "bg-rose-50 text-rose-800 ring-rose-200 dark:bg-rose-950 dark:text-rose-200 dark:ring-rose-800",
    ring: "border-rose-500",
    fill: "#f43f5e",
  },
  unchecked: {
    dot: "bg-slate-400",
    badge: "bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700",
    ring: "border-slate-300 dark:border-slate-600",
    fill: "#94a3b8",
  },
};

export function StateBadge({ state }: { state?: State }) {
  const { state: label } = useI18n();
  const s = STATE_STYLE[state ?? "unchecked"];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${s.badge}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} />
      {label(state)}
    </span>
  );
}

export function Reasons({ reasons }: { reasons?: Reason[] }) {
  const { reason } = useI18n();
  if (!reasons?.length) return null;
  return (
    <ul className="mt-1 space-y-0.5 text-sm text-slate-600 dark:text-slate-400">
      {reasons.map((r, i) => (
        <li key={i}>{reason(r)}</li>
      ))}
    </ul>
  );
}

export function Cause({ code, summary }: { code: string; summary?: string }) {
  const { t } = useI18n();
  return (
    <span
      title={summary}
      className="inline-flex rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-300"
    >
      {t(causeKey(code))}
    </span>
  );
}

export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900 ${className}`}
    >
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between gap-3">
          {title && (
            <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h2>
          )}
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  href,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  /** Makes the number a link to the list behind it. */
  href?: string;
}) {
  const box =
    "rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900";
  const body = (
    <>
      <div className="flex items-center justify-between text-xs font-medium text-slate-500 dark:text-slate-400">
        {label}
        {href && (
          <span aria-hidden className="text-slate-400 group-hover:text-sky-600">
            →
          </span>
        )}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-900 dark:text-slate-50">
        {value}
      </div>
    </>
  );
  if (!href) {
    return (
      <div className={box}>
        {body}
        {hint && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</div>}
      </div>
    );
  }
  // The whole card is the link; a hint that is a link of its own sits above it.
  return (
    <div className={`group relative transition hover:border-sky-300 hover:shadow-md ${box}`}>
      <Link href={href} className="block before:absolute before:inset-0">
        {body}
      </Link>
      {hint && (
        <div className="relative mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</div>
      )}
    </div>
  );
}

export function Progress({ value, total, label }: { value: number; total: number; label: string }) {
  const pct = total ? Math.round((value / total) * 100) : 0;
  return (
    <div>
      <div className="flex justify-between text-sm">
        <span className="text-slate-700 dark:text-slate-300">{label}</span>
        <span className="tabular-nums text-slate-500 dark:text-slate-400">
          {value}/{total}
        </span>
      </div>
      <div className="mt-1.5 h-2 rounded-full bg-slate-100 dark:bg-slate-800">
        <div
          className={`h-2 rounded-full ${pct === 100 ? "bg-emerald-500" : pct >= 60 ? "bg-sky-500" : "bg-amber-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function PageHeader({ title, lead }: { title: string; lead?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
        {title}
      </h1>
      {lead && (
        <p className="mt-1 max-w-3xl text-slate-600 dark:text-slate-400">
          <Rich text={lead} />
        </p>
      )}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 p-6 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-400">
      {children}
    </div>
  );
}

export function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.85em] text-slate-800 dark:bg-slate-800 dark:text-slate-200">
      {children}
    </code>
  );
}

/** Renders text with `backticks` as inline code (used in translated sentences). */
export function Rich({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((p, i) => (p.startsWith("`") ? <Code key={i}>{p.slice(1, -1)}</Code> : p))}
    </>
  );
}

export function Loader<T>({
  loaded,
  children,
}: {
  loaded: Loaded<T>;
  children: (data: T) => React.ReactNode;
}) {
  const { t } = useI18n();
  if (loaded.loading && !loaded.data) {
    return <div className="animate-pulse text-sm text-slate-500">{t("common.loading")}</div>;
  }
  if (loaded.error && !loaded.data) {
    return (
      <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200">
        {t("common.error")}: {loaded.error.message}{" "}
        <button className="ml-2 underline" onClick={loaded.reload}>
          {t("common.retry")}
        </button>
      </div>
    );
  }
  return loaded.data ? <>{children(loaded.data)}</> : null;
}

export function MemberLink({ id, children }: { id: string; children?: React.ReactNode }) {
  return (
    <Link
      href={`/catalog/?id=${encodeURIComponent(id)}`}
      className="font-medium text-sky-700 hover:underline dark:text-sky-400"
    >
      {children ?? id.split(":").slice(1).join(":")}
    </Link>
  );
}
