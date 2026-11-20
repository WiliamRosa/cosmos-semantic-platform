"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { QueryExplorer } from "@/components/QueryExplorer";
import { Empty, Loader, PageHeader, Rich } from "@/components/ui";
import { DEMO, endpoints } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";

export default function ExplorePage() {
  return (
    <Suspense>
      <Explore />
    </Suspense>
  );
}

function Explore() {
  const { t } = useI18n();
  return (
    <>
      <PageHeader title={t("explore.title")} lead={t("explore.lead")} />
      {DEMO ? (
        <Empty>
          <Rich text={t("explore.demo")} />
        </Empty>
      ) : (
        <Members />
      )}
    </>
  );
}

/** Loads what a request can ask for; `?metric=` preselects a metric. */
function Members() {
  const params = useSearchParams();
  const loaded = useLoad(endpoints.members, "explore:members");
  return (
    <Loader loaded={loaded}>
      {(members) => <QueryExplorer members={members} initialMetric={params.get("metric") ?? undefined} />}
    </Loader>
  );
}
