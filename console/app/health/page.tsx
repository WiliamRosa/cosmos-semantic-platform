"use client";

import { CheckCircle2, XCircle } from "lucide-react";
import { useState } from "react";
import { ReconciliationTable } from "@/components/ReconciliationTable";
import { Pagination } from "@/components/table";
import { Card, Empty, ListSkeleton, Loader, MemberLink, PageHeader, Rich } from "@/components/ui";
import { endpoints } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";
import { pageInfo } from "@/lib/table";

const SIZE = 10;

export default function HealthPage() {
  const { t, date, count } = useI18n();
  const [page, setPage] = useState(1);
  // The server pages the runs, so the whole history stays reachable.
  const loaded = useLoad(() => endpoints.runs({ offset: (page - 1) * SIZE, limit: SIZE }), `runs:${page}`);
  return (
    <>
      <PageHeader title={t("health.title")} lead={t("health.lead")} />
      <Loader
        loaded={loaded}
        skeleton={
          <Card>
            <ListSkeleton rows={5} />
          </Card>
        }
      >
        {({ runs, total }) => {
          if (total === 0) {
            return (
              <Empty>
                <Rich text={t("overview.noCheck")} />
              </Empty>
            );
          }
          const info = pageInfo(total, page, SIZE);
          return (
            <div className={`space-y-6 transition-opacity ${loaded.loading ? "opacity-60" : ""}`}>
              <div className="text-sm text-slate-500" aria-live="polite">
                {count(total, "table.result", "table.results")}
              </div>
              {runs.map((run) => {
                const bad =
                  run.rule_tests.failed > 0 ||
                  run.reconciliations.some((r) => r.outcome === "fail" || r.outcome === "error");
                const Icon = bad ? XCircle : CheckCircle2;
                return (
                  <Card
                    key={run.run_id}
                    title={
                      <span className="flex items-center gap-2">
                        <Icon
                          className={`h-4 w-4 ${bad ? "text-rose-500" : "text-emerald-500"}`}
                          aria-label={bad ? t("overview.checkFail") : t("overview.checkOk")}
                        />
                        {date(run.at)}
                        {run.data_source ? ` · ${run.data_source}` : ""}
                      </span>
                    }
                    action={<span className="font-mono text-xs text-slate-400">{run.run_id}</span>}
                  >
                    <div className="mb-4 text-sm">
                      <span className="font-medium">{t("health.ruleTests")}:</span>{" "}
                      <span className="tabular-nums">
                        {run.rule_tests.passed}/{run.rule_tests.total}
                      </span>
                      {run.rule_tests.failures.map((f) => (
                        <div key={`${f.rule}${f.test}`} className="mt-1 text-rose-700 dark:text-rose-300">
                          <MemberLink id={`rule:${f.rule}`}>{f.rule}</MemberLink> #{f.test}: {f.detail}
                        </div>
                      ))}
                    </div>
                    {run.reconciliations.length > 0 && <ReconciliationTable cases={run.reconciliations} />}
                  </Card>
                );
              })}
              {info.pages > 1 && (
                <div className="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
                  <Pagination
                    page={info.page}
                    pages={info.pages}
                    from={info.from}
                    to={info.to}
                    total={total}
                    size={SIZE}
                    onPage={(p) => {
                      setPage(p);
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                  />
                </div>
              )}
            </div>
          );
        }}
      </Loader>
    </>
  );
}
