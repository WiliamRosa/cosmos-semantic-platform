// Client for the console endpoints (/v1/console/*). Same origin in production (served by
// `cosmos serve`); NEXT_PUBLIC_COSMOS_API points the dev server at a running API.

export type State = "healthy" | "attention" | "failing" | "unchecked";

export type Reason =
  | { code: "rule_tests_fail"; failed: number; total: number }
  | { code: "rule_tests_pass"; total: number }
  | { code: "no_tests" }
  | { code: "never_reconciled" }
  | { code: "reconciliation_failed"; case: string; title: string }
  | { code: "reconciled"; count: number }
  | { code: "depends_on_failing"; member: string };

export type Labels = Record<string, string>;

export interface RunSummary {
  run_id: string;
  at: string;
  ok: boolean;
  data_source: string | null;
  rule_tests: { total: number; passed: number; failed: number };
  reconciliations: { pass: number; fail: number; error: number; skipped: number };
  duration_ms: number | null;
}

export interface Overview {
  timezone: string;
  model_hash: string;
  counts: Record<
    "models" | "metrics" | "measures" | "rules" | "concepts" | "processes" | "systems" | "teams",
    number
  >;
  governance: {
    metrics_with_owner: number;
    metrics_described: number;
    rules_with_owner: number;
    rules_with_tests: number;
    rule_tests_total: number;
    rule_tests_failed: number;
    metric_health: Record<State, number>;
  };
  latest_check: RunSummary | null;
  checks: RunSummary[];
  usage: {
    available: boolean;
    queries_30d: number;
    agent_queries_30d: number;
    errors_30d: number;
    p50_ms: number | null;
    p95_ms: number | null;
    by_day: { day: string; queries: number; agent: number; errors: number }[];
    top_metrics: { metric: string; queries: number }[];
  };
  viewer: { subject: string | null; admin: boolean };
}

export interface Process {
  name: string;
  description: string | null;
  owner: string | null;
  systems: string[];
  state: State;
  reasons: Reason[];
  metrics: number;
  rules: number;
  failing: number;
}

export interface GraphNode {
  id: string;
  kind: string;
  name: string;
  description?: string;
  owner?: string;
  state?: State;
  reasons?: Reason[];
  queries?: number;
  [key: string]: unknown;
}

export interface GraphData {
  focus: string | null;
  nodes: GraphNode[];
  edges: { source: string; target: string; relation: string }[];
}

export interface CatalogItem {
  id: string;
  kind: "metric" | "measure" | "rule" | "concept";
  name: string;
  description: string | null;
  owner?: string | null;
  state?: State;
  reasons?: Reason[];
  concepts?: string[];
  type?: string;
  model?: string;
  queries?: number;
  versions?: number;
  tests?: number;
  label?: Labels;
  synonyms?: Record<string, string[]>;
  maps_to?: string | null;
}

export interface AnswerRow {
  query_id: string;
  at: string;
  subject: string | null;
  channel: string | null;
  metrics: string[];
  dimensions: string[];
  filters: string[];
  as_of: string | null;
  data_source: string | null;
  rules: string[];
  outcome: "ok" | "error";
  error: string | null;
  duration_ms: number | null;
  row_count: number | null;
}

export interface RuleExplained {
  name: string;
  rule_kind?: string;
  description?: string | null;
  owner?: string | null;
  effective_from?: string | null;
  text?: string;
  missing?: boolean;
}

export interface AnswerDetail extends Omit<AnswerRow, "rules"> {
  request: Record<string, unknown>;
  user_context: Record<string, unknown> | null;
  sql: string | null;
  hashes: Record<string, string | null>;
  model_changed: boolean;
  formulas: Record<string, string | null>;
  rules: RuleExplained[];
}

export interface Reconciliation {
  id: string;
  title: string;
  metrics: string[];
  dimensions: string[];
  outcome: "pass" | "fail" | "error" | "skipped";
  status?: "match" | "divergent";
  causes?: { code: string; summary: string }[];
  rows?: Record<string, number>;
  totals?: { cosmos: number; legacy: number; difference: number; difference_pct: number | null };
  detail?: string;
  at?: string;
  run_id?: string;
}

export interface Run {
  run_id: string;
  at: string;
  model_hash: string;
  data_source: string | null;
  duration_ms: number;
  rule_tests: {
    total: number;
    passed: number;
    failed: number;
    failures: { rule: string; test: number; version: string | null; detail: string }[];
  };
  reconciliations: Reconciliation[];
}

export interface Member {
  detail: Record<string, unknown> & { kind: string; name: string };
  health: { state: State; reasons: Reason[] } | null;
  formula?: string;
  tests?: { total: number; failed: number; failures: string[] };
  impact?: {
    affected: Record<string, string[]>;
    owners: Record<string, string[]>;
    source_systems: string[];
    consumers: { subject: string; channel: string; queries: number }[];
  } | null;
  reconciliations?: Reconciliation[];
  recent_answers: AnswerRow[];
}

const KEY = "cosmos.credential";

export function getCredential(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setCredential(value: string): void {
  try {
    if (value) localStorage.setItem(KEY, value);
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: the credential lasts for this page only */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function headers(): HeadersInit {
  const credential = getCredential();
  if (!credential) return {};
  // A JWT from the identity provider goes as a bearer token; anything else is an API key.
  return credential.split(".").length === 3
    ? { Authorization: `Bearer ${credential}` }
    : { "X-API-Key": credential };
}

const BASE = process.env.NEXT_PUBLIC_COSMOS_API ?? "";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers(), ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const endpoints = {
  overview: () => api<Overview>("/v1/console/overview"),
  processes: () => api<Process[]>("/v1/console/processes"),
  graph: (focus?: string) =>
    api<GraphData>(`/v1/console/graph${focus ? `?focus=${encodeURIComponent(focus)}` : ""}`),
  catalog: () => api<{ items: CatalogItem[] }>("/v1/console/catalog"),
  member: (id: string) => api<Member>(`/v1/console/members/${encodeURIComponent(id)}`),
  answers: (metric?: string) =>
    api<{ scope: "everyone" | "yours"; answers: AnswerRow[] }>(
      `/v1/console/answers${metric ? `?metric=${encodeURIComponent(metric)}` : ""}`,
    ),
  answer: (id: string) => api<AnswerDetail>(`/v1/console/answers/${encodeURIComponent(id)}`),
  replay: (id: string) =>
    api<{ verdict: string; explanation: string; matches: Record<string, boolean> }>(
      `/v1/replay/${encodeURIComponent(id)}`,
      { method: "POST" },
    ),
  runs: () => api<{ available: boolean; runs: Run[] }>("/v1/console/runs"),
  usage: () => api<Record<string, unknown> & { available: boolean }>("/v1/console/usage"),
};
