"use client";

// Ctrl+K (⌘K on a Mac): jump to any page, metric, measure, rule, concept or process by typing.
// It loads the catalog and the processes the first time it opens.

import { CornerDownLeft, FileText, Search, Workflow } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { endpoints, type State } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";
import { StateBadge } from "./ui";

export interface PaletteItem {
  id: string;
  group: string;
  title: string;
  hint?: string;
  href: string;
  state?: State;
}

export function CommandPalette({
  pages,
  onClose,
}: {
  pages: { href: string; title: string }[];
  onClose: () => void;
}) {
  const { t, label } = useI18n();
  const router = useRouter();
  const catalog = useLoad(endpoints.catalog, "palette:catalog");
  const processes = useLoad(endpoints.processes, "palette:processes");
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const items = useMemo<PaletteItem[]>(() => {
    const all: PaletteItem[] = pages.map((p) => ({
      id: `page:${p.href}`,
      group: t("search.pages"),
      title: p.title,
      href: p.href,
    }));
    for (const i of catalog.data?.items ?? []) {
      all.push({
        id: i.id,
        group: t(`kind.${i.kind}` as Key),
        title: i.kind === "concept" ? label(i.label, i.name) : i.name,
        hint: i.description ?? undefined,
        href: `/catalog/?id=${encodeURIComponent(i.id)}`,
        state: i.kind === "concept" ? undefined : i.state,
      });
    }
    for (const p of processes.data ?? []) {
      all.push({
        id: `process:${p.name}`,
        group: t("kind.process"),
        title: p.name,
        hint: p.description ?? undefined,
        href: `/processes/?name=${encodeURIComponent(p.name)}`,
        state: p.state,
      });
    }
    return all;
  }, [pages, catalog.data, processes.data, t, label]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items.slice(0, 12);
    const scored = items
      .map((item) => {
        const title = item.title.toLowerCase();
        const score = title.startsWith(q)
          ? 0
          : title.includes(q)
            ? 1
            : `${item.hint ?? ""} ${item.group}`.toLowerCase().includes(q)
              ? 2
              : -1;
        return { item, score };
      })
      .filter((s) => s.score >= 0);
    scored.sort((a, b) => a.score - b.score || a.item.title.localeCompare(b.item.title));
    return scored.slice(0, 40).map((s) => s.item);
  }, [items, query]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const open = (item: PaletteItem | undefined) => {
    if (!item) return;
    onClose();
    router.push(item.href);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, shown.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      open(shown[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  const loading = (catalog.loading && !catalog.data) || (processes.loading && !processes.data);

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center bg-slate-900/50 p-4 pt-[12vh] backdrop-blur-sm"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("search.placeholder")}
        className="w-full max-w-xl overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl dark:border-slate-700 dark:bg-slate-900"
      >
        <div className="flex items-center gap-3 border-b border-slate-200 px-4 dark:border-slate-800">
          <Search className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={t("search.placeholder")}
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={shown[active] ? `palette-${active}` : undefined}
            className="h-12 w-full bg-transparent text-sm outline-none placeholder:text-slate-400 focus-visible:outline-none"
          />
          <kbd className="rounded border border-slate-200 px-1.5 text-[11px] text-slate-400 dark:border-slate-700">
            Esc
          </kbd>
        </div>
        <ul id="palette-list" ref={listRef} role="listbox" className="max-h-[50vh] overflow-y-auto p-2">
          {shown.length === 0 && !loading && (
            <li className="px-3 py-8 text-center text-sm text-slate-500">{t("search.empty")}</li>
          )}
          {shown.map((item, i) => {
            const Icon = item.id.startsWith("page:")
              ? FileText
              : item.id.startsWith("process:")
                ? Workflow
                : null;
            return (
              <li
                key={item.id}
                id={`palette-${i}`}
                data-index={i}
                role="option"
                aria-selected={i === active}
                onMouseMove={() => setActive(i)}
                onClick={() => open(item)}
                className={`flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm ${
                  i === active ? "bg-slate-100 dark:bg-slate-800" : ""
                }`}
              >
                <span className="w-24 shrink-0 truncate text-[11px] font-medium uppercase tracking-wide text-slate-400">
                  {item.group}
                </span>
                {Icon && <Icon className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium text-slate-900 dark:text-slate-100">{item.title}</span>
                  {item.hint && <span className="block truncate text-xs text-slate-500">{item.hint}</span>}
                </span>
                {item.state && <StateBadge state={item.state} />}
                {i === active && <CornerDownLeft className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />}
              </li>
            );
          })}
          {loading && (
            <li className="px-3 py-3 text-xs text-slate-500" role="status">
              {t("common.loading")}
            </li>
          )}
        </ul>
        <div className="border-t border-slate-200 px-4 py-2 text-[11px] text-slate-500 dark:border-slate-800">
          {t("search.hint")}
        </div>
      </div>
    </div>
  );
}
