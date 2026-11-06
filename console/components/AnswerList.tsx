"use client";

import { Bot, User } from "lucide-react";
import Link from "next/link";
import type { AnswerRow } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export function AnswerList({ answers }: { answers: AnswerRow[] }) {
  const { t, date, number, count } = useI18n();
  return (
    <ul className="divide-y divide-slate-100 dark:divide-slate-800">
      {answers.map((a) => {
        const Icon = a.channel === "mcp" ? Bot : User;
        return (
          <li key={a.query_id ?? a.at}>
            <Link
              href={a.query_id ? `/answers/?id=${encodeURIComponent(a.query_id)}` : "/answers/"}
              className="flex items-start gap-3 px-1 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50"
            >
              <Icon className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {a.metrics.join(", ") || "—"}
                  {a.dimensions.length > 0 && (
                    <span className="font-normal text-slate-500"> · {a.dimensions.join(", ")}</span>
                  )}
                </div>
                <div className="text-xs text-slate-500">
                  {a.subject ?? "—"} · {a.channel} · {date(a.at)}
                  {a.data_source ? ` · ${a.data_source}` : ""}
                </div>
              </div>
              <div className="shrink-0 text-right text-xs">
                {a.outcome === "ok" ? (
                  <span className="tabular-nums text-slate-600 dark:text-slate-400">
                    {count(a.row_count, "answers.row", "answers.rows")} · {number(a.duration_ms)} ms
                  </span>
                ) : (
                  <span className="text-rose-700 dark:text-rose-300">{t("answers.failed")}</span>
                )}
              </div>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
