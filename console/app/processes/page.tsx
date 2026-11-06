"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { ProcessGraph } from "@/components/ProcessGraph";
import { Card, Empty, Loader, PageHeader, Reasons, StateBadge } from "@/components/ui";
import { endpoints } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";

export default function ProcessesPage() {
  return (
    <Suspense>
      <Processes />
    </Suspense>
  );
}

function Processes() {
  const { t } = useI18n();
  const name = useSearchParams().get("name");
  const loaded = useLoad(endpoints.processes, "processes");

  if (name) {
    const focus = name === "*" ? undefined : `process:${name}`;
    return (
      <>
        <Link href="/processes/" className="text-sm text-sky-700 hover:underline dark:text-sky-400">
          ← {t("common.back")}
        </Link>
        <PageHeader
          title={name === "*" ? t("processes.whole") : `${t("processes.map")}: ${name}`}
          lead={t("processes.legend")}
        />
        <ProcessGraph focus={focus} />
      </>
    );
  }

  return (
    <>
      <PageHeader title={t("processes.title")} lead={t("processes.lead")} />
      <Loader loaded={loaded}>
        {(processes) =>
          processes.length === 0 ? (
            <Empty>{t("processes.empty")}</Empty>
          ) : (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {processes.map((p) => (
                <Link key={p.name} href={`/processes/?name=${encodeURIComponent(p.name)}`}>
                  <Card className="h-full transition hover:border-sky-300 hover:shadow-md">
                    <div className="flex items-start justify-between gap-2">
                      <h2 className="font-semibold">{p.name}</h2>
                      <StateBadge state={p.state} />
                    </div>
                    {p.description && (
                      <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">{p.description}</p>
                    )}
                    <Reasons reasons={p.reasons} />
                    <dl className="mt-4 grid grid-cols-2 gap-1 text-sm">
                      <dt className="text-slate-500">{t("common.owner")}</dt>
                      <dd>{p.owner ?? t("common.noOwner")}</dd>
                      <dt className="text-slate-500">{t("processes.systems")}</dt>
                      <dd>{p.systems.join(", ") || t("common.none")}</dd>
                      <dt className="text-slate-500">{t("processes.uses")}</dt>
                      <dd className="tabular-nums">
                        {p.metrics} · {p.rules}
                      </dd>
                    </dl>
                  </Card>
                </Link>
              ))}
              <Link href="/processes/?name=*">
                <Card className="grid h-full place-items-center border-dashed text-sm text-slate-600 hover:border-sky-300 dark:text-slate-400">
                  {t("processes.whole")} →
                </Card>
              </Link>
            </div>
          )
        }
      </Loader>
    </>
  );
}
