"use client";

import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Pagination, SortHeader } from "@/components/table";
import { Card, Empty, Loader, MemberLink, PageHeader, Rich } from "@/components/ui";
import { ApiError, endpoints } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";
import { nextSort, paginate, type Sort, sortRows } from "@/lib/table";

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
              <CartesianGrid strokeDasharray="3 3" horizontal={false} />
              <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
              <YAxis type="category" dataKey="metric" width={140} tick={{ fontSize: 11 }} />
              <Tooltip />
              <Bar dataKey="queries" name={t("common.queries")} fill="#0ea5e9" radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="-mx-5 -mb-5 mt-4 border-t border-slate-200 dark:border-slate-800">
          <UsageTable
            rows={report.by_metric.map((m) => ({ name: m.metric, link: `metric:${m.metric}`, ...m }))}
            money={money}
          />
        </div>
      </Card>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title={t("usage.byPrincipal")}>
          <div className="-mx-5 -mb-5">
            <UsageTable
              rows={report.by_principal.map((p) => ({
                name: `${p.subject ?? "—"} (${p.channel})`,
                ...p,
              }))}
              money={money}
            />
          </div>
        </Card>
        <Card title={t("usage.bySource")}>
          <div className="-mx-5 -mb-5">
            <UsageTable rows={report.by_data_source.map((s) => ({ name: s.data_source, ...s }))} money={money} />
          </div>
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

type Column = "name" | "queries" | "avg_ms" | "cost";
const SIZE = 10;

/** A usage table sorted by queries (most first) and paged ten rows at a time. */
function UsageTable({
  rows,
  money,
}: {
  rows: (Bucket & { name: string; link?: string })[];
  money: (v: number) => string;
}) {
  const { t, number } = useI18n();
  const [sort, setSort] = useState<Sort<Column> | null>({ key: "queries", dir: "desc" });
  const [page, setPage] = useState(1);
  const sorted = useMemo(
    () => sortRows(rows, sort, (r, key) => (key === "name" ? r.name : (r[key] as number))),
    [rows, sort],
  );
  const paged = paginate(sorted, page, SIZE);
  const onSort = (column: Column) => {
    setSort(nextSort(sort, column, column === "name" ? "asc" : "desc"));
    setPage(1);
  };
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 dark:border-slate-800">
            <tr>
              <SortHeader label={t("table.name")} column="name" sort={sort} onSort={onSort} />
              <SortHeader label={t("usage.queries")} column="queries" sort={sort} onSort={onSort} align="right" />
              <SortHeader label={t("usage.avg")} column="avg_ms" sort={sort} onSort={onSort} align="right" />
              <SortHeader label={t("usage.cost")} column="cost" sort={sort} onSort={onSort} align="right" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {paged.rows.map((r) => (
              <tr key={r.name} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                <td className="max-w-[16rem] truncate px-4 py-2">
                  {r.link ? <MemberLink id={r.link}>{r.name}</MemberLink> : r.name}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{number(r.queries)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{number(r.avg_ms)} ms</td>
                <td className="px-4 py-2 text-right tabular-nums">{money(r.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {paged.pages > 1 && (
        <Pagination
          page={paged.page}
          pages={paged.pages}
          from={paged.from}
          to={paged.to}
          total={sorted.length}
          size={SIZE}
          onPage={setPage}
        />
      )}
    </>
  );
}
