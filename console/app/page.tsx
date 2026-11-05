"use client";

import Link from "next/link";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Card,
  Empty,
  Loader,
  MemberLink,
  PageHeader,
  Progress,
  Rich,
  STATE_STYLE,
  Stat,
} from "@/components/ui";
import { endpoints, type Overview, type State } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";

const STATES: State[] = ["failing", "attention", "unchecked", "healthy"];

export default function OverviewPage() {
  const { t } = useI18n();
  const loaded = useLoad(endpoints.overview, "overview");
  return (
    <>
      <PageHeader title={t("overview.title")} lead={t("overview.lead")} />
      <Loader loaded={loaded}>{(o) => <OverviewBody o={o} />}</Loader>
    </>
  );
}

function OverviewBody({ o }: { o: Overview }) {
  const { t, number, date, state: stateLabel } = useI18n();
  const g = o.governance;
  const totalMetrics = o.counts.metrics;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label={t("overview.metrics")} value={o.counts.metrics} hint={t("overview.measures", { n: o.counts.measures })} />
        <Stat label={t("overview.rules")} value={o.counts.rules} />
        <Stat label={t("overview.concepts")} value={o.counts.concepts} />
        <Stat label={t("overview.processes")} value={o.counts.processes} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title={t("overview.lastCheck")} className="lg:col-span-1">
          {o.latest_check ? (
            <div className="space-y-3">
              <div
                className={`rounded-lg p-3 text-sm font-medium ${
                  o.latest_check.ok
                    ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
                    : "bg-rose-50 text-rose-800 dark:bg-rose-950 dark:text-rose-200"
                }`}
              >
                {o.latest_check.ok ? t("overview.checkOk") : t("overview.checkFail")}
              </div>
              <dl className="grid grid-cols-2 gap-2 text-sm">
                <dt className="text-slate-500">{t("health.ruleTests")}</dt>
                <dd className="text-right tabular-nums">
                  {o.latest_check.rule_tests.passed}/{o.latest_check.rule_tests.total}
                </dd>
                <dt className="text-slate-500">{t("health.reconciliations")}</dt>
                <dd className="text-right tabular-nums">
                  {o.latest_check.reconciliations.pass}/
                  {o.latest_check.reconciliations.pass + o.latest_check.reconciliations.fail + o.latest_check.reconciliations.error}
                </dd>
              </dl>
              <div className="text-xs text-slate-500">
                {date(o.latest_check.at)}
                {o.latest_check.data_source ? ` · ${o.latest_check.data_source}` : ""}
              </div>
              <Link href="/health/" className="text-sm text-sky-700 hover:underline dark:text-sky-400">
                {t("nav.health")} →
              </Link>
            </div>
          ) : (
            <Empty>
              <Rich text={t("overview.noCheck")} />
            </Empty>
          )}
        </Card>

        <Card title={t("overview.metricHealth")}>
          <div className="flex h-3 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
            {STATES.map((s) =>
              g.metric_health[s] ? (
                <div
                  key={s}
                  style={{ width: `${(g.metric_health[s] / Math.max(totalMetrics, 1)) * 100}%`, background: STATE_STYLE[s].fill }}
                  title={`${stateLabel(s)}: ${g.metric_health[s]}`}
                />
              ) : null,
            )}
          </div>
          <ul className="mt-4 space-y-2 text-sm">
            {STATES.map((s) => (
              <li key={s} className="flex items-center justify-between">
                <span className="flex items-center gap-2">
                  <span className={`h-2.5 w-2.5 rounded-full ${STATE_STYLE[s].dot}`} />
                  {stateLabel(s)}
                </span>
                <span className="tabular-nums text-slate-600 dark:text-slate-400">{g.metric_health[s]}</span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title={t("overview.ownership")}>
          <div className="space-y-4">
            <Progress label={t("overview.withOwner")} value={g.metrics_with_owner} total={totalMetrics} />
            <Progress label={t("overview.described")} value={g.metrics_described} total={totalMetrics} />
            <Progress label={t("overview.rulesTested")} value={g.rules_with_tests} total={o.counts.rules} />
            <Progress
              label={t("overview.ruleTests")}
              value={g.rule_tests_total - g.rule_tests_failed}
              total={g.rule_tests_total}
            />
          </div>
        </Card>
      </div>

      <Card title={`${t("overview.usage")} · ${t("common.since")}`}>
        {!o.usage.available ? (
          <Empty>
            <Rich text={t("overview.noAudit")} />
          </Empty>
        ) : (
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="grid grid-cols-2 content-start gap-3 lg:col-span-1">
              <Stat label={t("overview.answers")} value={number(o.usage.queries_30d)} />
              <Stat label={t("overview.byAgents")} value={number(o.usage.agent_queries_30d)} />
              <Stat label={t("overview.errors")} value={number(o.usage.errors_30d)} />
              <Stat
                label={t("overview.latency")}
                value={o.usage.p50_ms === null ? "—" : `${number(o.usage.p50_ms)} ms`}
                hint={o.usage.p95_ms === null ? undefined : `p95 ${number(o.usage.p95_ms)} ms`}
              />
            </div>
            <div className="lg:col-span-2">
              <div className="mb-2 text-xs font-medium text-slate-500">{t("overview.daily")}</div>
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={o.usage.by_day.map((d) => ({ ...d, people: d.queries - d.agent }))}
                    margin={{ left: -20, right: 8, top: 4 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} tickFormatter={(d: string) => d.slice(5)} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="agent" name={t("overview.agents")} stackId="1" fill="#0ea5e9" maxBarSize={48} />
                    <Bar dataKey="people" name={t("overview.people")} stackId="1" fill="#94a3b8" maxBarSize={48} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
            {o.usage.top_metrics.length > 0 && (
              <div className="lg:col-span-3">
                <div className="mb-2 text-xs font-medium text-slate-500">{t("overview.topMetrics")}</div>
                <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                  {o.usage.top_metrics.map((m) => (
                    <li
                      key={m.metric}
                      className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800"
                    >
                      <MemberLink id={`metric:${m.metric}`}>{m.metric}</MemberLink>
                      <span className="tabular-nums text-slate-500">{number(m.queries)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Card>

      {o.checks.length > 1 && (
        <Card title={t("overview.checks")}>
          <div className="h-40">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={o.checks.map((c) => ({
                  at: c.at.slice(5, 16).replace("T", " "),
                  pass: c.reconciliations.pass + c.rule_tests.passed,
                  fail: c.reconciliations.fail + c.reconciliations.error + c.rule_tests.failed,
                }))}
                margin={{ left: -20, right: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="at" tick={{ fontSize: 10 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="pass" name={t("health.outcome.pass")} stackId="1" fill="#10b981" />
                <Bar dataKey="fail" name={t("health.outcome.fail")} stackId="1" fill="#f43f5e" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}
    </div>
  );
}
