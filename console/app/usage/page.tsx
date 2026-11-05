"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card, Empty, Loader, MemberLink, PageHeader, Rich } from "@/components/ui";
import { ApiError, endpoints } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";

interface Bucket {
  queries: number;
  errors: number;
  denied: number;
  avg_ms: number;
  cost: number;
  [key: string]: unknown;
}

export default function UsagePage() {
  const { t } = useI18n();
  const loaded = useLoad(endpoints.usage, "usage");
  if (loaded.error instanceof ApiError && loaded.error.status === 403) {
    return (
      <>
        <PageHeader title={t("usage.title")} />
        <Empty>
          <Rich text={t("usage.forbidden")} />
        </Empty>
      </>
    );
  }
  return (
    <>
      <PageHeader title={t("usage.title")} lead={t("usage.lead")} />
      <Loader loaded={loaded}>
        {(report) =>
          !report.available ? (
            <Empty>
              <Rich text={t("overview.noAudit")} />
            </Empty>
          ) : (
            <UsageBody report={report as unknown as Report} />
          )
        }
      </Loader>
    </>
  );
}

interface Report {
  currency: string | null;
  total: Bucket;
  by_metric: (Bucket & { metric: string; principals: number; agent_queries: number })[];
  by_principal: (Bucket & { subject: string; channel: string })[];
  by_data_source: (Bucket & { data_source: string })[];
  unused_metrics?: string[];
}

function UsageBody({ report }: { report: Report }) {
  const { t, number } = useI18n();
  const money = (v: number) =>
    report.currency ? `${number(v, 4)} ${report.currency}` : number(v, 4);
  return (
    <div className="space-y-6">
      <Card title={t("usage.byMetric")}>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={report.by_metric.slice(0, 12)} layout="vertical" margin={{ left: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" horizontal={false} />
              <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
              <YAxis type="category" dataKey="metric" width={140} tick={{ fontSize: 11 }} />
              <Tooltip />
              <Bar dataKey="queries" name={t("common.queries")} fill="#0ea5e9" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title={t("usage.byPrincipal")}>
          <Table
            rows={report.by_principal.map((p) => ({
              name: `${p.subject ?? "—"} (${p.channel})`,
              ...p,
            }))}
            money={money}
          />
        </Card>
        <Card title={t("usage.bySource")}>
          <Table rows={report.by_data_source.map((s) => ({ name: s.data_source, ...s }))} money={money} />
        </Card>
      </div>
      {report.unused_metrics && report.unused_metrics.length > 0 && (
        <Card title={t("usage.unused")}>
          <ul className="flex flex-wrap gap-2 text-sm">
            {report.unused_metrics.map((m) => (
              <li key={m} className="rounded-lg border border-slate-200 px-2.5 py-1 dark:border-slate-700">
                <MemberLink id={`metric:${m}`}>{m}</MemberLink>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function Table({ rows, money }: { rows: (Bucket & { name: string })[]; money: (v: number) => string }) {
  const { t, number } = useI18n();
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-xs text-slate-500">
          <th className="py-1.5 font-medium" />
          <th className="py-1.5 text-right font-medium">{t("common.queries")}</th>
          <th className="py-1.5 text-right font-medium">{t("usage.avg")}</th>
          <th className="py-1.5 text-right font-medium">{t("usage.cost")}</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
        {rows.map((r) => (
          <tr key={r.name}>
            <td className="py-1.5 pr-2">{r.name}</td>
            <td className="py-1.5 text-right tabular-nums">{number(r.queries)}</td>
            <td className="py-1.5 text-right tabular-nums">{number(r.avg_ms)} ms</td>
            <td className="py-1.5 text-right tabular-nums">{money(r.cost)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
