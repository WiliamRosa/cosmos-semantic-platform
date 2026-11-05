"use client";

import type { Reconciliation } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { Cause, MemberLink } from "./ui";

const OUTCOME_STYLE: Record<Reconciliation["outcome"], string> = {
  pass: "text-emerald-700 dark:text-emerald-300",
  fail: "text-rose-700 dark:text-rose-300",
  error: "text-rose-700 dark:text-rose-300",
  skipped: "text-slate-500",
};

export function ReconciliationTable({
  cases,
  showDate = false,
}: {
  cases: Reconciliation[];
  showDate?: boolean;
}) {
  const { t, number, date } = useI18n();
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-slate-500">
            <th className="py-2 pr-4 font-medium">{t("health.case")}</th>
            <th className="py-2 pr-4 font-medium">{t("health.outcome")}</th>
            <th className="py-2 pr-4 font-medium">{t("health.causes")}</th>
            <th className="py-2 text-right font-medium">{t("health.difference")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {cases.map((c, i) => (
            <tr key={`${c.run_id ?? ""}${c.id}${i}`} className="align-top">
              <td className="py-2 pr-4">
                <div className="font-medium">
                  {c.id} · {c.title}
                </div>
                <div className="text-xs text-slate-500">
                  {c.metrics.map((m) => (
                    <MemberLink key={m} id={`metric:${m}`}>
                      {m}
                    </MemberLink>
                  ))}
                  {showDate && c.at ? ` · ${date(c.at)}` : ""}
                </div>
              </td>
              <td className={`py-2 pr-4 font-medium ${OUTCOME_STYLE[c.outcome]}`}>
                {t(`health.outcome.${c.outcome}` as Key)}
                {c.detail && <div className="text-xs font-normal">{c.detail}</div>}
              </td>
              <td className="py-2 pr-4">
                <div className="flex flex-wrap gap-1">
                  {(c.causes ?? []).map((cause) => (
                    <Cause key={cause.code} code={cause.code} summary={cause.summary} />
                  ))}
                </div>
              </td>
              <td className="py-2 text-right tabular-nums">
                {c.totals?.difference_pct != null
                  ? `${c.totals.difference_pct > 0 ? "+" : ""}${number(c.totals.difference_pct, 2)}%`
                  : c.totals
                    ? number(c.totals.difference, 2)
                    : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
