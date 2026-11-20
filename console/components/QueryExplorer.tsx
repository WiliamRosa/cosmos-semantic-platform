"use client";

// The query explorer (DEC-41): pick a metric, dimensions, a time grain and filters, then run
// the request through the public API (`POST /v1/query`) or preview its SQL (`POST /v1/sql`).
// It sends a structured request, never SQL: filters are built from a dimension, an operator
// and a value, and the server checks them like any other request. The same credential, row
// filters and audit apply as for any API client. Everything shown is rendered as text.

import { Play, Plus, SquareTerminal, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  endpoints,
  type Members,
  type QueryRequest,
  type QueryResult,
  type SqlPreview,
} from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import { Card, Code, Empty, Rich } from "./ui";

type Dimension = Members["dimensions"][number];

const GRAINS = ["hour", "day", "week", "month", "quarter", "year"] as const;
const OPERATORS = ["=", "<>", ">", ">=", "<", "<="] as const;
type Operator = (typeof OPERATORS)[number];
const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 10000;

interface FilterRow {
  id: number;
  dimension: string;
  op: Operator;
  value: string;
}

/** The operators that make sense for a dimension type. */
function operatorsFor(type: string | undefined): readonly Operator[] {
  return type === "time" || type === "number" ? OPERATORS : (["=", "<>"] as const);
}

/**
 * A filter row as a request expression (`{model.dimension} op literal`), or null when the value
 * is missing or does not fit the dimension's type. Strings are quoted with '' escaping; the
 * server parses and checks the expression again.
 */
export function filterExpression(row: FilterRow, type: string | undefined): string | null {
  const ref = `{${row.dimension}}`;
  const value = row.value.trim();
  if (!row.dimension || !value || !operatorsFor(type).includes(row.op)) return null;
  if (type === "number") {
    const n = Number(value);
    return Number.isFinite(n) ? `${ref} ${row.op} ${n}` : null;
  }
  if (type === "boolean") {
    return value === "true" || value === "false" ? `${ref} ${row.op} ${value}` : null;
  }
  if (type === "time") {
    return /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${ref} ${row.op} '${value}'` : null;
  }
  return `${ref} ${row.op} '${value.replaceAll("'", "''")}'`;
}

type Outcome =
  | { kind: "result"; result: QueryResult }
  | { kind: "sql"; preview: SqlPreview }
  | { kind: "error"; message: string };

export function QueryExplorer({ members, initialMetric }: { members: Members; initialMetric?: string }) {
  const { t } = useI18n();
  const { requireAuth } = useSession();

  const metrics = useMemo(
    () => [...members.metrics].sort((a, b) => a.name.localeCompare(b.name)),
    [members.metrics],
  );
  const byName = useMemo(
    () => new Map(members.dimensions.map((d) => [d.name, d] as const)),
    [members.dimensions],
  );
  const timeDimensions = members.dimensions.filter((d) => d.type === "time");
  const otherDimensions = members.dimensions.filter((d) => d.type !== "time");
  const predicateRules = members.rules.filter((r) => r.filter);

  const [metric, setMetric] = useState(
    metrics.some((m) => m.name === initialMetric) ? initialMetric! : (metrics[0]?.name ?? ""),
  );
  const [timeDimension, setTimeDimension] = useState("");
  const [grain, setGrain] = useState<string>("month");
  const [dimensions, setDimensions] = useState<string[]>([]);
  const [filters, setFilters] = useState<FilterRow[]>([]);
  const [rules, setRules] = useState<string[]>([]);
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [busy, setBusy] = useState<"query" | "sql" | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [nextId, setNextId] = useState(1);

  const built = useMemo(() => {
    const expressions = filters.map((f) => filterExpression(f, byName.get(f.dimension)?.type));
    const time = timeDimension ? (grain ? `${timeDimension}__${grain}` : timeDimension) : null;
    const request: QueryRequest = {
      metrics: [metric],
      dimensions: [...(time ? [time] : []), ...dimensions],
      // Periods in order; without a time dimension the server's order stands.
      ...(time ? { order_by: [time] } : {}),
      filters: [
        ...expressions.filter((e): e is string => e !== null),
        ...rules.map((r) => `{rule.${r}}`),
      ],
      limit,
    };
    return { request, incomplete: expressions.some((e) => e === null) };
  }, [metric, timeDimension, grain, dimensions, filters, rules, limit, byName]);

  const valid = Boolean(metric) && !built.incomplete && limit >= 1 && limit <= MAX_LIMIT;

  async function submit(kind: "query" | "sql") {
    if (!valid) return;
    setBusy(kind);
    try {
      setOutcome(
        kind === "query"
          ? { kind: "result", result: await endpoints.query(built.request) }
          : { kind: "sql", preview: await endpoints.sql(built.request) },
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) requireAuth();
      setOutcome({ kind: "error", message: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  const field =
    "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 dark:border-slate-700 dark:bg-slate-800";
  const label = "mb-1 block text-xs font-medium text-slate-500";
  const ghost =
    "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50 disabled:opacity-50 dark:text-slate-300 dark:ring-slate-700 dark:hover:bg-slate-800";
  const selected = metrics.find((m) => m.name === metric);

  return (
    <div className="space-y-6">
      <Card title={t("explore.request")}>
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label htmlFor="explore-metric" className={label}>
              {t("kind.metric")}
            </label>
            <select id="explore-metric" value={metric} onChange={(e) => setMetric(e.target.value)} className={field}>
              {metrics.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                </option>
              ))}
            </select>
            {selected?.description && <p className="mt-1 text-xs text-slate-500">{selected.description}</p>}
          </div>
          <div className="grid grid-cols-[1fr_auto] gap-2">
            <div>
              <label htmlFor="explore-time" className={label}>
                {t("explore.time")}
              </label>
              <select
                id="explore-time"
                value={timeDimension}
                onChange={(e) => setTimeDimension(e.target.value)}
                className={field}
              >
                <option value="">{t("common.none")}</option>
                {timeDimensions.map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="explore-grain" className={label}>
                {t("explore.grain")}
              </label>
              <select
                id="explore-grain"
                value={grain}
                disabled={!timeDimension}
                onChange={(e) => setGrain(e.target.value)}
                className={`${field} disabled:opacity-50`}
              >
                <option value="">{t("explore.grain.none")}</option>
                {GRAINS.map((g) => (
                  <option key={g} value={g}>
                    {t(`explore.grain.${g}` as Key)}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        <div className="mt-5">
          <span className={label}>{t("explore.dimensions")}</span>
          <div className="flex flex-wrap items-center gap-2">
            {dimensions.map((d) => (
              <span
                key={d}
                className="inline-flex items-center gap-1 rounded-lg bg-slate-100 px-2.5 py-1 text-sm dark:bg-slate-800"
              >
                {d}
                <button
                  type="button"
                  onClick={() => setDimensions(dimensions.filter((x) => x !== d))}
                  aria-label={`${t("explore.remove")} ${d}`}
                  className="text-slate-500 hover:text-slate-900 dark:hover:text-white"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </span>
            ))}
            <select
              aria-label={t("explore.addDimension")}
              value=""
              onChange={(e) => e.target.value && setDimensions([...dimensions, e.target.value])}
              className={`${field} w-auto`}
            >
              <option value="">{t("explore.addDimension")}</option>
              {otherDimensions
                .filter((d) => !dimensions.includes(d.name))
                .map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name}
                    {d.sensitivity ? ` (${d.sensitivity})` : ""}
                  </option>
                ))}
            </select>
          </div>
        </div>

        <div className="mt-5">
          <span className={label}>{t("explore.filters")}</span>
          <div className="space-y-2">
            {filters.map((f) => (
              <FilterEditor
                key={f.id}
                row={f}
                dimension={byName.get(f.dimension)}
                dimensions={members.dimensions}
                field={field}
                onChange={(next) => setFilters(filters.map((x) => (x.id === f.id ? next : x)))}
                onRemove={() => setFilters(filters.filter((x) => x.id !== f.id))}
              />
            ))}
            <button
              type="button"
              className={ghost}
              onClick={() => {
                const first = members.dimensions[0];
                setFilters([...filters, { id: nextId, dimension: first?.name ?? "", op: "=", value: "" }]);
                setNextId(nextId + 1);
              }}
            >
              <Plus className="h-4 w-4" aria-hidden />
              {t("explore.addFilter")}
            </button>
          </div>
          {predicateRules.length > 0 && (
            <fieldset className="mt-3">
              <legend className="mb-1 text-xs text-slate-500">{t("explore.rules")}</legend>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {predicateRules.map((r) => (
                  <label key={r.name} className="inline-flex items-center gap-2 text-sm" title={r.description ?? undefined}>
                    <input
                      type="checkbox"
                      checked={rules.includes(r.name)}
                      onChange={(e) =>
                        setRules(e.target.checked ? [...rules, r.name] : rules.filter((x) => x !== r.name))
                      }
                    />
                    {r.name}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
        </div>

        <div className="mt-5 flex flex-wrap items-end gap-3 border-t border-slate-100 pt-4 dark:border-slate-800">
          <div>
            <label htmlFor="explore-limit" className={label}>
              {t("explore.limit")}
            </label>
            <input
              id="explore-limit"
              type="number"
              min={1}
              max={MAX_LIMIT}
              value={limit}
              onChange={(e) => setLimit(Math.trunc(Number(e.target.value)) || 0)}
              className={`${field} w-28`}
            />
          </div>
          <div className="ml-auto flex gap-2">
            <button type="button" className={ghost} disabled={!valid || busy !== null} onClick={() => submit("sql")}>
              <SquareTerminal className="h-4 w-4" aria-hidden />
              {busy === "sql" ? t("common.loading") : t("explore.showSql")}
            </button>
            <button
              type="button"
              disabled={!valid || busy !== null}
              onClick={() => submit("query")}
              className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-white dark:text-slate-900"
            >
              <Play className="h-4 w-4" aria-hidden />
              {busy === "query" ? t("explore.running") : t("explore.run")}
            </button>
          </div>
        </div>
        {built.incomplete && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">{t("explore.incomplete")}</p>}
      </Card>

      <div aria-live="polite">
        {outcome?.kind === "error" && (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200">
            {t("explore.refused")}: <Rich text={outcome.message} />
          </div>
        )}
        {outcome?.kind === "sql" && (
          <SqlCard sql={outcome.preview.sql} note={`${t("explore.previewOnly")} · ${outcome.preview.dialect}`} />
        )}
        {outcome?.kind === "result" && <ResultView result={outcome.result} limit={limit} />}
        {!outcome && <Empty>{t("explore.empty")}</Empty>}
      </div>
    </div>
  );
}

function FilterEditor({
  row,
  dimension,
  dimensions,
  field,
  onChange,
  onRemove,
}: {
  row: FilterRow;
  dimension: Dimension | undefined;
  dimensions: Dimension[];
  field: string;
  onChange: (row: FilterRow) => void;
  onRemove: () => void;
}) {
  const { t } = useI18n();
  const type = dimension?.type;
  const ops = operatorsFor(type);
  const [suggestions, setSuggestions] = useState<string[]>([]);

  // Known values of a categorical dimension, as suggestions (audited like any request).
  useEffect(() => {
    let alive = true;
    setSuggestions([]);
    if (type !== "categorical" || !row.dimension) return;
    endpoints
      .dimensionValues(row.dimension)
      .then((r) => alive && setSuggestions((r.values ?? []).map((v) => String(v.value))))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [row.dimension, type]);

  const listId = `explore-values-${row.id}`;
  return (
    <div className="grid gap-2 sm:grid-cols-[minmax(0,2fr)_6rem_minmax(0,2fr)_auto]">
      <select
        aria-label={t("kind.dimension")}
        value={row.dimension}
        onChange={(e) => onChange({ ...row, dimension: e.target.value, op: "=", value: "" })}
        className={field}
      >
        {dimensions.map((d) => (
          <option key={d.name} value={d.name}>
            {d.name}
          </option>
        ))}
      </select>
      <select
        aria-label={t("explore.operator")}
        value={row.op}
        onChange={(e) => onChange({ ...row, op: e.target.value as Operator })}
        className={field}
      >
        {ops.map((op) => (
          <option key={op} value={op}>
            {op}
          </option>
        ))}
      </select>
      {type === "boolean" ? (
        <select
          aria-label={t("explore.value")}
          value={row.value}
          onChange={(e) => onChange({ ...row, value: e.target.value })}
          className={field}
        >
          <option value="">—</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : (
        <>
          <input
            aria-label={t("explore.value")}
            type={type === "time" ? "date" : type === "number" ? "number" : "text"}
            list={suggestions.length ? listId : undefined}
            value={row.value}
            onChange={(e) => onChange({ ...row, value: e.target.value })}
            className={field}
          />
          {suggestions.length > 0 && (
            <datalist id={listId}>
              {suggestions.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          )}
        </>
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label={t("explore.remove")}
        className="grid h-9 w-9 place-items-center rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

function SqlCard({ sql, note }: { sql: string; note?: string }) {
  const { t } = useI18n();
  return (
    <Card title={t("answers.sql")} action={note && <span className="text-xs text-slate-500">{note}</span>}>
      <pre className="max-h-96 overflow-auto rounded-lg bg-slate-950 p-4 text-xs leading-relaxed text-slate-100">
        {sql}
      </pre>
    </Card>
  );
}

function cell(value: unknown, number: (v: number, digits?: number) => string): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return number(value, 2);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function ResultView({ result, limit }: { result: QueryResult; limit: number }) {
  const { t, number, count, date } = useI18n();
  const e = result.evidence;
  const hashes: [string, string][] = [
    ["model", e.model_hash],
    ["sql", e.sql.hash],
    ["result", e.result.hash],
  ];
  const context = Object.entries(e.principal.user_context ?? {});
  return (
    <div className="space-y-6">
      <Card
        title={t("answers.result")}
        action={
          <span className="text-xs text-slate-500">
            {count(result.row_count, "answers.row", "answers.rows")} · {e.data_source.name ?? ""} ({e.data_source.dialect})
          </span>
        }
      >
        {result.rows.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-slate-800">
                  {result.columns.map((c) => (
                    <th key={c.name} scope="col" className={`px-3 py-2 font-medium ${c.kind === "metric" ? "text-right" : ""}`}>
                      {c.member}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row, i) => (
                  <tr key={i} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                    {result.columns.map((c) => (
                      <td
                        key={c.name}
                        className={`px-3 py-1.5 ${c.kind === "metric" ? "text-right tabular-nums" : ""}`}
                      >
                        {cell(row[c.name], number)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>{t("explore.noRows")}</Empty>
        )}
        {result.row_count >= limit && <p className="mt-3 text-xs text-slate-500">{t("explore.atLimit")}</p>}
      </Card>

      <Card
        title={t("explore.evidence")}
        action={
          <Link
            href={`/answers/?id=${encodeURIComponent(e.query_id)}`}
            className="text-sm font-medium text-sky-700 hover:underline dark:text-sky-400"
          >
            {t("explore.openAnswer")}
          </Link>
        }
      >
        <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs text-slate-500">{t("explore.queryId")}</dt>
            <dd className="font-mono text-xs">{e.query_id}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">{t("answers.asOf")}</dt>
            <dd>
              {e.as_of ?? "—"} · {e.timezone} · {date(e.generated_at)}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-xs text-slate-500">{t("explore.formula")}</dt>
            <dd>
              {e.metrics.map((m) => (
                <div key={m.name}>
                  {m.name}: {m.formula ? <Code>{m.formula}</Code> : "—"}
                </div>
              ))}
            </dd>
          </div>
        </dl>

        <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">{t("answers.rules")}</h3>
        {e.rules.length ? (
          <ul className="mt-2 space-y-2 text-sm">
            {e.rules.map((r) => (
              <li key={r.name}>
                <span className="font-medium">{r.name}</span>{" "}
                <span className="text-xs text-slate-500">
                  ({r.used_as.join(", ")} · {t("member.effective")} {r.effective_from ?? "—"} ·{" "}
                  {t("explore.tests", { passed: r.tests.passed, total: r.tests.total })})
                </span>
                <div className="mt-0.5">
                  <Code>{r.logic}</Code>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-slate-500">{t("common.none")}</p>
        )}

        <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">{t("explore.policies")}</h3>
        <div className="mt-2 space-y-1 text-sm">
          <div>
            {t("answers.context")}:{" "}
            {context.length
              ? context.map(([k, v]) => (
                  <span key={k} className="mr-2">
                    {k} = <Code>{String(v)}</Code>
                  </span>
                ))
              : t("common.none")}
          </div>
          {e.policies.row_filters.length ? (
            <ul className="space-y-1">
              {e.policies.row_filters.map((f) => (
                <li key={`${f.model}.${f.name}`}>
                  {f.model}.{f.name}: <Code>{f.filter}</Code>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-slate-500">{t("explore.noRowFilters")}</div>
          )}
        </div>

        <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">{t("answers.fingerprints")}</h3>
        <dl className="mt-2 grid gap-3 text-sm sm:grid-cols-3">
          {hashes.map(([k, v]) => (
            <div key={k} className="min-w-0">
              <dt className="text-xs text-slate-500">{k}</dt>
              <dd className="font-mono text-xs break-all">
                <button
                  type="button"
                  title={`${v}\n${t("answers.copy")}`}
                  onClick={() => navigator.clipboard?.writeText(v)}
                  className="rounded px-1 py-0.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  {v.slice(0, 16)}…
                </button>
              </dd>
            </div>
          ))}
        </dl>

        {e.caveats.length > 0 && (
          <>
            <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">{t("explore.caveats")}</h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-600 dark:text-slate-400">
              {e.caveats.map((c) => (
                <li key={c}>
                  <Rich text={c} />
                </li>
              ))}
            </ul>
          </>
        )}

        <details className="mt-5">
          <summary className="cursor-pointer text-sm text-slate-500">{t("explore.rawEvidence")}</summary>
          <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-slate-100 p-4 text-xs dark:bg-slate-800">
            {JSON.stringify(e, null, 2)}
          </pre>
        </details>
      </Card>

      <SqlCard sql={e.sql.text} note={e.data_source.dialect} />
    </div>
  );
}
