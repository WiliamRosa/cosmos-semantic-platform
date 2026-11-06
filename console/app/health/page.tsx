"use client";

import { ReconciliationTable } from "@/components/ReconciliationTable";
import { Card, Empty, Loader, MemberLink, PageHeader, Rich } from "@/components/ui";
import { endpoints } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";

export default function HealthPage() {
  const { t, date } = useI18n();
  const loaded = useLoad(endpoints.runs, "runs");
  return (
    <>
      <PageHeader title={t("health.title")} lead={t("health.lead")} />
      <Loader loaded={loaded}>
        {({ runs }) =>
          runs.length === 0 ? (
            <Empty>
              <Rich text={t("overview.noCheck")} />
            </Empty>
          ) : (
            <div className="space-y-6">
              {runs.map((run) => {
                const bad =
                  run.rule_tests.failed > 0 ||
                  run.reconciliations.some((r) => r.outcome === "fail" || r.outcome === "error");
                return (
                  <Card
                    key={run.run_id}
                    title={
                      <span className="flex items-center gap-2">
                        <span className={`h-2.5 w-2.5 rounded-full ${bad ? "bg-rose-500" : "bg-emerald-500"}`} />
                        {date(run.at)}
                        {run.data_source ? ` · ${run.data_source}` : ""}
                      </span>
                    }
                    action={<span className="font-mono text-xs text-slate-400">{run.run_id}</span>}
                  >
                    <div className="mb-4 text-sm">
                      <span className="font-medium">{t("health.ruleTests")}:</span>{" "}
                      {run.rule_tests.passed}/{run.rule_tests.total}
                      {run.rule_tests.failures.map((f) => (
                        <div key={`${f.rule}${f.test}`} className="mt-1 text-rose-700 dark:text-rose-300">
                          <MemberLink id={`rule:${f.rule}`}>{f.rule}</MemberLink> #{f.test}: {f.detail}
                        </div>
                      ))}
                    </div>
                    {run.reconciliations.length > 0 && (
                      <ReconciliationTable cases={run.reconciliations} />
                    )}
                  </Card>
                );
              })}
            </div>
          )
        }
      </Loader>
    </>
  );
}
