"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { AnswerList } from "@/components/AnswerList";
import { Pagination, TableFrame } from "@/components/table";
import {
  BackLink,
  Card,
  Code,
  Empty,
  ListSkeleton,
  Loader,
  MemberLink,
  PageHeader,
  Rich,
} from "@/components/ui";
import { type AnswerDetail, ApiError, endpoints } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";
import { PAGE_SIZES, pageInfo } from "@/lib/table";

export default function AnswersPage() {
  return (
    <Suspense>
      <Answers />
    </Suspense>
  );
}

const DEFAULT_SIZE = 25;

function Answers() {
  const params = useSearchParams();
  const id = params.get("id");
  if (id) return <AnswerView id={id} />;
  return <AnswerPages />;
}

/** The audited answers, newest first; the server pages them, so every answer is reachable. */
function AnswerPages() {
  const { t, count } = useI18n();
  const params = useSearchParams();
  const router = useRouter();
  const metric = params.get("metric") ?? undefined;
  const page = Math.max(1, Number(params.get("page")) || 1);
  const size = PAGE_SIZES.find((s) => s === Number(params.get("size"))) ?? DEFAULT_SIZE;
  const loaded = useLoad(
    () => endpoints.answers({ metric, offset: (page - 1) * size, limit: size }),
    `answers:${metric ?? ""}:${page}:${size}`,
  );
  const go = (next: { page?: number; size?: number; metric?: string | null }) => {
    const q = new URLSearchParams();
    const m = next.metric === undefined ? metric : next.metric;
    const s = next.size ?? size;
    const p = next.page ?? 1;
    if (m) q.set("metric", m);
    if (p > 1) q.set("page", String(p));
    if (s !== DEFAULT_SIZE) q.set("size", String(s));
    const search = q.toString();
    router.replace(`/answers/${search ? `?${search}` : ""}`, { scroll: false });
  };
  return (
    <>
      <PageHeader title={t("answers.title")} lead={t("answers.lead")} />
      {metric && (
        <div className="mb-4 flex items-center gap-2 text-sm">
          <span className="inline-flex items-center gap-1.5 rounded-lg bg-sky-50 px-3 py-1.5 text-sky-800 ring-1 ring-sky-200 dark:bg-sky-950 dark:text-sky-200 dark:ring-sky-800">
            {t("kind.metric")}: <strong className="font-medium">{metric}</strong>
            <button onClick={() => go({ metric: null })} aria-label={t("catalog.clearFilters")}>
              ×
            </button>
          </span>
        </div>
      )}
      <Loader
        loaded={loaded}
        skeleton={
          <TableFrame>
            <ListSkeleton rows={8} />
          </TableFrame>
        }
      >
        {(data) => {
          const info = pageInfo(data.total, page, size);
          return (
            <>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-500">
                <span>{data.scope === "everyone" ? t("answers.scopeAll") : t("answers.scopeMine")}</span>
                <span aria-live="polite">{count(data.total, "table.result", "table.results")}</span>
              </div>
              <TableFrame
                footer={
                  <Pagination
                    page={info.page}
                    pages={info.pages}
                    from={info.from}
                    to={info.to}
                    total={data.total}
                    size={size}
                    onPage={(p) => go({ page: p })}
                    onSize={(s) => go({ size: s })}
                  />
                }
              >
                <div className={`px-3 transition-opacity ${loaded.loading ? "opacity-60" : ""}`}>
                  {data.answers.length ? (
                    <AnswerList answers={data.answers} />
                  ) : (
                    <div className="p-3">
                      <Empty>{t("answers.empty")}</Empty>
                    </div>
                  )}
                </div>
              </TableFrame>
            </>
          );
        }}
      </Loader>
    </>
  );
}

function AnswerView({ id }: { id: string }) {
  const { t } = useI18n();
  const loaded = useLoad(() => endpoints.answer(id), `answer:${id}`);
  return (
    <>
      <BackLink href="/answers/">{t("common.back")}</BackLink>
      <div className="mt-2">
        <Loader loaded={loaded}>{(a) => <AnswerBody a={a} />}</Loader>
      </div>
    </>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 py-2 sm:grid-cols-[200px_1fr]">
      <dt className="text-sm text-slate-500">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  );
}

function AnswerBody({ a }: { a: AnswerDetail }) {
  const { t, date, number, count } = useI18n();
  const [replay, setReplay] = useState<{ verdict: string } | { error: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function reproduce() {
    setBusy(true);
    try {
      setReplay(await endpoints.replay(a.query_id));
    } catch (err) {
      if (err instanceof ApiError && err.status === 501) setReplay({ error: t("answers.demoReplay") });
      else setReplay({ error: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-xs text-slate-500">{a.query_id}</div>
        <h1 className="text-2xl font-semibold tracking-tight">{a.metrics.join(", ")}</h1>
        <div className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          {a.subject} · {a.channel} · {date(a.at)}
        </div>
      </div>

      {a.model_changed && (
        <div className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {t("answers.modelChanged")}
        </div>
      )}

      <Card title={t("answers.what")}>
        <dl className="divide-y divide-slate-100 dark:divide-slate-800">
          {a.metrics.map((m) => (
            <Row key={m} label={m}>
              <MemberLink id={`metric:${m}`}>{m}</MemberLink>
              {a.formulas[m] && (
                <div className="mt-1">
                  <Code>{a.formulas[m]}</Code>
                </div>
              )}
            </Row>
          ))}
          {a.dimensions.length > 0 && <Row label={t("kind.dimension")}>{a.dimensions.join(", ")}</Row>}
          {a.filters.length > 0 && (
            <Row label="Filters">
              {a.filters.map((f) => (
                <div key={f}>
                  <Code>{f}</Code>
                </div>
              ))}
            </Row>
          )}
          <Row label={t("answers.asOf")}>{a.as_of ?? "—"}</Row>
          <Row label={t("answers.context")}>
            {a.user_context && Object.keys(a.user_context).length
              ? Object.entries(a.user_context).map(([k, v]) => (
                  <div key={k}>
                    {k} = <Code>{String(v)}</Code>
                  </div>
                ))
              : t("common.none")}
          </Row>
          <Row label={t("answers.result")}>
            {a.outcome === "ok"
              ? `${count(a.row_count, "answers.row", "answers.rows")} · ${number(a.duration_ms)} ms · ${a.data_source ?? ""}`
              : <span className="text-rose-700 dark:text-rose-300">{a.error}</span>}
          </Row>
        </dl>
      </Card>

      {a.rules.length > 0 && (
        <Card title={t("answers.rules")}>
          <ul className="space-y-3">
            {a.rules.map((r) => (
              <li key={r.name} className="text-sm">
                <MemberLink id={`rule:${r.name}`}>{r.name}</MemberLink>
                {r.text && <div className="text-slate-600 dark:text-slate-400">{r.text}</div>}
                <div className="text-xs text-slate-500">
                  {t("common.owner")}: {r.owner ?? t("common.noOwner")}
                  {r.effective_from ? ` · ${t("member.effective")} ${r.effective_from}` : ""}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {a.outcome === "ok" && (
        <Card
          title={t("answers.fingerprints")}
          action={
            <button
              onClick={reproduce}
              disabled={busy}
              className="rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-white dark:text-slate-900"
            >
              {busy ? t("answers.replaying") : t("answers.replay")}
            </button>
          }
        >
          <dl className="grid gap-3 text-sm sm:grid-cols-3">
            {Object.entries(a.hashes).map(([k, v]) => (
              <div key={k} className="min-w-0">
                <dt className="text-xs text-slate-500">{k.replace("_hash", "")}</dt>
                <dd className="font-mono text-xs break-all">
                  {v ? (
                    <button
                      type="button"
                      title={`${v}\n${t("answers.copy")}`}
                      onClick={() => navigator.clipboard?.writeText(v)}
                      className="rounded px-1 py-0.5 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                    >
                      {v.slice(0, 16)}…
                    </button>
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {replay && (
            <div
              className={`mt-4 rounded-lg p-3 text-sm ${
                "verdict" in replay && replay.verdict === "identical"
                  ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
                  : "bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-200"
              }`}
            >
              {"verdict" in replay ? t(`answers.verdict.${replay.verdict}` as Key) : <Rich text={replay.error} />}
            </div>
          )}
        </Card>
      )}

      {a.sql && (
        <Card title={t("answers.sql")}>
          <pre className="max-h-96 overflow-auto rounded-lg bg-slate-950 p-4 text-xs leading-relaxed text-slate-100">
            {a.sql}
          </pre>
        </Card>
      )}
    </div>
  );
}
