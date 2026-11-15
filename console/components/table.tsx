"use client";

// Building blocks for lists: sortable column headers, a pager, a search box that `/` focuses,
// and a toolbar row. Pages compose them with lib/table.ts.

import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { useI18n } from "@/lib/i18n";
import { PAGE_SIZES, type Sort } from "@/lib/table";

export function SortHeader<K extends string>({
  label,
  column,
  sort,
  onSort,
  align = "left",
  className = "",
}: {
  label: string;
  column: K;
  sort: Sort<K> | null;
  onSort: (column: K) => void;
  align?: "left" | "right";
  className?: string;
}) {
  const { t } = useI18n();
  const active = sort?.key === column;
  const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <th
      scope="col"
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      className={`px-4 py-2.5 text-xs font-medium text-slate-500 dark:text-slate-400 ${
        align === "right" ? "text-right" : "text-left"
      } ${className}`}
    >
      <button
        type="button"
        onClick={() => onSort(column)}
        title={t("table.sortBy", { column: label })}
        className={`inline-flex items-center gap-1 rounded hover:text-slate-900 dark:hover:text-slate-100 ${
          align === "right" ? "flex-row-reverse" : ""
        } ${active ? "text-slate-900 dark:text-slate-100" : ""}`}
      >
        {label}
        <Icon className={`h-3.5 w-3.5 ${active ? "" : "opacity-40"}`} aria-hidden />
      </button>
    </th>
  );
}

export function Pagination({
  page,
  pages,
  from,
  to,
  total,
  size,
  onPage,
  onSize,
}: {
  page: number;
  pages: number;
  from: number;
  to: number;
  total: number;
  size: number;
  onPage: (page: number) => void;
  onSize?: (size: number) => void;
}) {
  const { t, number } = useI18n();
  if (total === 0) return null;
  const button =
    "inline-grid h-8 w-8 place-items-center rounded-lg text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-300 dark:ring-slate-700 dark:hover:bg-slate-800";
  return (
    <nav
      aria-label={t("table.page", { page, pages })}
      className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 px-4 py-3 text-sm text-slate-600 dark:border-slate-800 dark:text-slate-400"
    >
      <div className="tabular-nums">
        {t("table.range", { from: number(from), to: number(to), total: number(total) })}
      </div>
      <div className="flex items-center gap-3">
        {onSize && (
          <label className="hidden items-center gap-2 sm:flex">
            <span>{t("table.pageSize")}</span>
            <select
              value={size}
              onChange={(e) => onSize(Number(e.target.value))}
              className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
            >
              {PAGE_SIZES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
        <span className="tabular-nums">{t("table.page", { page, pages })}</span>
        <div className="flex gap-1">
          <button
            type="button"
            className={button}
            onClick={() => onPage(page - 1)}
            disabled={page <= 1}
            aria-label={t("table.previous")}
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            type="button"
            className={button}
            onClick={() => onPage(page + 1)}
            disabled={page >= pages}
            aria-label={t("table.next")}
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    </nav>
  );
}

/** A search box; `/` anywhere on the page focuses it (unless typing in another field). */
export function SearchInput({
  value,
  onChange,
  placeholder,
  className = "",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  className?: string;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className={`relative w-full max-w-sm ${className}`}>
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" aria-hidden />
      <input
        ref={ref}
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && (onChange(""), ref.current?.blur())}
        placeholder={placeholder}
        aria-label={placeholder}
        title={t("search.focus")}
        className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-14 text-sm shadow-sm outline-none placeholder:text-slate-400 focus-visible:outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 dark:border-slate-700 dark:bg-slate-900 [&::-webkit-search-cancel-button]:hidden"
      />
      {value ? (
        <button
          type="button"
          onClick={() => onChange("")}
          aria-label={t("catalog.clearFilters")}
          className="absolute right-2 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
        >
          <X className="h-4 w-4" />
        </button>
      ) : (
        <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border border-slate-200 px-1.5 font-sans text-[11px] text-slate-400 dark:border-slate-700">
          /
        </kbd>
      )}
    </div>
  );
}

/** The frame of a list: a bordered surface with a scrollable table and an optional pager. */
export function TableFrame({ children, footer }: { children: React.ReactNode; footer?: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <div className="overflow-x-auto">{children}</div>
      {footer}
    </div>
  );
}
