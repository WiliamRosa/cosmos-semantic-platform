"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { AnswerList } from "@/components/AnswerList";
import { ReconciliationTable } from "@/components/ReconciliationTable";
import {
  Card,
  Code,
  Empty,
  Loader,
  MemberLink,
  PageHeader,
  Reasons,
  StateBadge,
} from "@/components/ui";
import { type CatalogItem, endpoints, type Labels, type Member, type State } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";

export default function CatalogPage() {
  return (
    <Suspense>
      <Catalog />
    </Suspense>
  );
}

function Catalog() {
  const id = useSearchParams().get("id");
  return id ? <MemberView id={id} /> : <CatalogList />;
}

const KINDS = ["metric", "measure", "rule", "concept"] as const;
const STATES: State[] = ["failing", "attention", "unchecked", "healthy"];

function CatalogList() {
  const { t, label, state: stateLabel } = useI18n();
  const loaded = useLoad(endpoints.catalog, "catalog");
  const params = useSearchParams();
  const router = useRouter();
  const [query, setQuery] = useState("");
  // Kind and state live in the URL, so the overview can link to a filtered list.
  const kind = KINDS.find((k) => k === params.get("kind")) ?? "all";
  const state = STATES.find((s) => s === params.get("state")) ?? null;
  const go = (next: { kind?: string; state?: string | null }) => {
    const q = new URLSearchParams();
    const k = next.kind ?? kind;
    const s = next.state === undefined ? state : next.state;
    if (k !== "all") q.set("kind", k);
    if (s) q.set("state", s);
    const search = q.toString();
    router.replace(`/catalog/${search ? `?${search}` : ""}`, { scroll: false });
  };
  const setKind = (k: string) => go({ kind: k });
  return (
    <>
      <PageHeader title={t("catalog.title")} lead={t("catalog.lead")} />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("catalog.filter")}
          className="w-full max-w-sm rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-sky-500 dark:border-slate-700 dark:bg-slate-900"
        />
        {(["all", ...KINDS] as const).map((k) => (
          <button
            key={k}
            onClick={() => setKind(k)}
            className={`rounded-lg px-3 py-1.5 text-sm ${
              kind === k
                ? "bg-slate-900 text-white dark:bg-white dark:text-slate-900"
                : "bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-400 dark:ring-slate-800"
            }`}
          >
            {k === "all" ? t("common.all") : t(`kind.${k}` as Key)}
          </button>
        ))}
        {state && (
          <button
            onClick={() => go({ state: null })}
            className="inline-flex items-center gap-1.5 rounded-lg bg-sky-50 px-3 py-1.5 text-sm text-sky-800 ring-1 ring-sky-200 hover:bg-sky-100 dark:bg-sky-950 dark:text-sky-200 dark:ring-sky-800"
            title={t("catalog.clearState")}
          >
            {stateLabel(state)} <span aria-hidden>×</span>
          </button>
        )}
      </div>
      <Loader loaded={loaded}>
        {({ items }) => (
          <Filtered items={items} query={query} kind={kind} state={state} label={label} />
        )}
      </Loader>
    </>
  );
}

function Filtered({
  items,
  query,
  kind,
  state,
  label,
}: {
  items: CatalogItem[];
  query: string;
  kind: string;
  state: State | null;
  label: (l: Labels | undefined, f: string) => string;
}) {
  const { t, count } = useI18n();
  const shown = useMemo(() => {
    const q = query.toLowerCase();
    return items.filter(
      (i) =>
        (kind === "all" || i.kind === kind) &&
        (!state || i.state === state) &&
        (!q ||
          [i.name, i.owner, i.description, label(i.label, ""), ...(i.concepts ?? [])]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(q))),
    );
  }, [items, query, kind, state, label]);
  if (!shown.length) return <Empty>{t("common.none")}</Empty>;
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
      <table className="w-full text-sm">
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {shown.map((i) => (
            <tr key={i.id} className="align-top hover:bg-slate-50 dark:hover:bg-slate-800/50">
              <td className="px-4 py-3">
                <div className="text-[10px] font-medium uppercase tracking-wide text-slate-500">
                  {t(`kind.${i.kind}` as Key)}
                  {i.type ? ` · ${i.type}` : ""}
                </div>
                <MemberLink id={i.id}>{i.kind === "concept" ? label(i.label, i.name) : i.name}</MemberLink>
                {i.description && (
                  <div className="mt-0.5 max-w-xl text-slate-600 dark:text-slate-400">{i.description}</div>
                )}
              </td>
              <td className="hidden px-4 py-3 text-slate-600 md:table-cell dark:text-slate-400">
                {i.kind === "concept" ? i.maps_to : (i.owner ?? t("common.noOwner"))}
              </td>
              <td className="hidden px-4 py-3 text-right tabular-nums text-slate-500 sm:table-cell">
                {i.kind === "rule"
                  ? `${count(i.versions, "catalog.version", "catalog.versions")} · ${count(i.tests, "catalog.test", "catalog.tests")}`
                  : typeof i.queries === "number"
                    ? count(i.queries, "common.query", "common.queries")
                    : ""}
              </td>
              <td className="px-4 py-3 text-right">
                {i.kind !== "concept" && <StateBadge state={i.state} />}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MemberView({ id }: { id: string }) {
  const { t } = useI18n();
  const loaded = useLoad(() => endpoints.member(id), `member:${id}`);
  return (
    <>
      <Link href="/catalog/" className="text-sm text-sky-700 hover:underline dark:text-sky-400">
        ← {t("common.back")}
      </Link>
      <div className="mt-2">
        <Loader loaded={loaded}>{(m) => <MemberBody m={m} />}</Loader>
      </div>
    </>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-800 dark:text-slate-200">{children}</dd>
    </div>
  );
}

function MemberBody({ m }: { m: Member }) {
  const { t, label, lang } = useI18n();
  const d = m.detail as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const kind = d.kind as string;
  const definition: string | undefined = d.text ?? d.definition ?? undefined;
  const affected = m.impact?.affected ?? {};
  const affectedItems = Object.entries(affected).flatMap(([group, names]) =>
    names
      .filter((n) => !(group === `${kind}s` && n === d.name))
      .map((n) => ({ group, n })),
  );
  return (
    <div className="space-y-6">
      <div>
        <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {t(`kind.${kind}` as Key)}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {kind === "concept" ? label(d.label, d.name) : d.name}
          </h1>
          {m.health && <StateBadge state={m.health.state} />}
        </div>
        {m.health && <Reasons reasons={m.health.reasons} />}
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title={t("member.definition")} className="lg:col-span-2">
          <dl className="space-y-4">
            {d.description && <Field label={t("common.description")}>{d.description}</Field>}
            {definition && definition !== d.description && (
              <Field label={t("member.definition")}>{definition}</Field>
            )}
            {m.formula && (
              <Field label={t("member.formula")}>
                <Code>{m.formula}</Code>
              </Field>
            )}
            {kind === "concept" && d.synonyms && (
              <Field label={t("catalog.synonyms")}>
                {(d.synonyms[lang] ?? d.synonyms.en ?? []).join(", ") || t("common.none")}
              </Field>
            )}
            {kind === "concept" && d.defined_by && (
              <Field label={t("catalog.mapsTo")}>
                <MemberLink id={`${d.defined_by.kind}:${d.defined_by.name}`}>{d.defined_by.name}</MemberLink>
              </Field>
            )}
            {d.effective_from && <Field label={t("member.effective")}>{d.effective_from}</Field>}
          </dl>
        </Card>
        <Card title={t("member.status")}>
          <dl className="space-y-4">
            <Field label={t("common.owner")}>{d.owner ?? t("common.noOwner")}</Field>
            {d.processes?.length > 0 && (
              <Field label={t("member.processes")}>
                {d.processes.map((p: string) => (
                  <Link key={p} href={`/processes/?name=${encodeURIComponent(p)}`} className="mr-2 text-sky-700 hover:underline dark:text-sky-400">
                    {p}
                  </Link>
                ))}
              </Field>
            )}
            {d.source_systems?.length > 0 && (
              <Field label={t("member.sources")}>{d.source_systems.join(", ")}</Field>
            )}
            {m.tests && (
              <Field label={t("member.tests")}>
                {m.tests.total - m.tests.failed}/{m.tests.total}
                {m.tests.failures.map((f, i) => (
                  <div key={i} className="text-rose-700 dark:text-rose-300">{f}</div>
                ))}
              </Field>
            )}
          </dl>
        </Card>
      </div>

      {kind === "rule" && d.history?.length > 0 && (
        <Card title={t("member.history")}>
          <ol className="relative space-y-4 border-l border-slate-200 pl-5 dark:border-slate-700">
            <li>
              <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-sky-500" />
              <div className="text-sm font-medium">
                {t("member.current")} · {d.effective_from ?? "—"}
              </div>
              <Code>{d.logic}</Code>
              {d.change_note && <div className="mt-1 text-sm text-slate-600 dark:text-slate-400">{d.change_note}</div>}
            </li>
            {d.history.map((h: { effective_from: string; logic: string; change_note?: string }) => (
              <li key={h.effective_from}>
                <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-slate-300" />
                <div className="text-sm font-medium">{h.effective_from}</div>
                <Code>{h.logic}</Code>
                {h.change_note && <div className="mt-1 text-sm text-slate-600 dark:text-slate-400">{h.change_note}</div>}
              </li>
            ))}
          </ol>
        </Card>
      )}

      {(kind === "metric" || kind === "measure") && (
        <Card title={t("member.reconciliations")}>
          {m.reconciliations?.length ? (
            <ReconciliationTable cases={m.reconciliations} showDate />
          ) : (
            <Empty>{t("member.noReconciliations")}</Empty>
          )}
        </Card>
      )}

      {m.impact !== undefined && (
        <Card title={t("member.impact")}>
          {affectedItems.length === 0 ? (
            <p className="text-sm text-slate-600 dark:text-slate-400">{t("member.noImpact")}</p>
          ) : (
            <div className="space-y-4">
              <ul className="flex flex-wrap gap-2">
                {affectedItems.map(({ group, n }) => {
                  const k = group.replace(/s$/, "").replace("row_filter", "rule");
                  const target = group === "processes" ? null : `${k}:${n}`;
                  return (
                    <li key={`${group}:${n}`} className="rounded-lg border border-slate-200 px-2.5 py-1 text-sm dark:border-slate-700">
                      <span className="mr-1 text-xs text-slate-500">{t(`kind.${group === "processes" ? "process" : k}` as Key)}</span>
                      {target && ["metric", "measure", "rule", "concept"].includes(k) ? (
                        <MemberLink id={target}>{n}</MemberLink>
                      ) : group === "processes" ? (
                        <Link href={`/processes/?name=${encodeURIComponent(n)}`} className="font-medium text-sky-700 hover:underline dark:text-sky-400">{n}</Link>
                      ) : (
                        n
                      )}
                    </li>
                  );
                })}
              </ul>
              {m.impact && Object.keys(m.impact.owners).length > 0 && (
                <Field label={t("member.owners")}>{Object.keys(m.impact.owners).join(", ")}</Field>
              )}
              {m.impact && m.impact.consumers.length > 0 && (
                <Field label={t("member.consumers")}>
                  {m.impact.consumers.map((c) => `${c.subject} (${c.channel})`).join(", ")}
                </Field>
              )}
            </div>
          )}
        </Card>
      )}

      {m.recent_answers.length > 0 && (
        <Card title={t("member.recent")}>
          <AnswerList answers={m.recent_answers} />
        </Card>
      )}
    </div>
  );
}
