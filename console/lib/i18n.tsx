"use client";

// Two languages: English (default) and Brazilian Portuguese. Every visible string goes
// through `t`; health reasons arrive from the API as codes and are worded here.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { Labels, Reason, State } from "./api";

export type Lang = "en" | "pt";

const en = {
  "app.title": "Cosmos Semantic",
  "app.subtitle": "Console",
  "nav.overview": "Overview",
  "nav.processes": "Processes",
  "nav.catalog": "Catalog",
  "nav.answers": "Answers",
  "nav.health": "Checks",
  "nav.usage": "Usage and cost",
  "lang.switch": "Português",
  "auth.title": "Sign in to the console",
  "auth.help":
    "Paste an API key or an access token from your identity provider. It stays in this browser only.",
  "auth.placeholder": "API key or token",
  "auth.save": "Connect",
  "auth.cancel": "Cancel",
  "auth.change": "Change credential",
  "auth.signout": "Sign out",
  "common.loading": "Loading…",
  "common.error": "Could not load",
  "common.retry": "Try again",
  "common.owner": "Owner",
  "common.noOwner": "No owner",
  "common.description": "Description",
  "common.none": "None",
  "common.search": "Search",
  "common.all": "All",
  "common.queries": "queries",
  "common.query": "query",
  "overview.measures": "+ {n} measures",
  "catalog.version": "version",
  "catalog.test": "test",
  "common.back": "Back",
  "common.open": "Open",
  "common.since": "Last 30 days",
  "state.healthy": "Healthy",
  "state.attention": "Needs attention",
  "state.failing": "Failing",
  "state.unchecked": "Not verified",
  "kind.metric": "Metric",
  "kind.measure": "Measure",
  "kind.rule": "Business rule",
  "kind.concept": "Concept",
  "kind.process": "Process",
  "kind.system": "System",
  "kind.team": "Team",
  "kind.model": "Data model",
  "kind.dimension": "Dimension",
  "overview.title": "Can we trust the numbers?",
  "overview.lead":
    "Governed metrics, the business rules behind them, whether they still match the legacy reports, and how AI agents use them.",
  "overview.metrics": "Governed metrics",
  "overview.rules": "Business rules",
  "overview.concepts": "Glossary concepts",
  "overview.processes": "Business processes",
  "overview.ownership": "Ownership and tests",
  "overview.withOwner": "Metrics with an owner",
  "overview.described": "Metrics with a description",
  "overview.rulesTested": "Rules with tests",
  "overview.ruleTests": "Rule tests passing",
  "overview.metricHealth": "Metric health",
  "overview.lastCheck": "Last check",
  "overview.noCheck":
    "No check has run yet. Schedule `cosmos check` (CI or cron) to record rule tests and reconciliations.",
  "overview.checkOk": "Everything verified passed",
  "overview.checkFail": "Something verified failed",
  "overview.usage": "Questions answered",
  "overview.noAudit":
    "No audit log is configured (COSMOS_AUDIT_LOG), so usage is not shown.",
  "overview.answers": "Answers",
  "overview.byAgents": "by AI agents",
  "overview.errors": "errors or denials",
  "overview.latency": "Typical time",
  "overview.topMetrics": "Most asked metrics",
  "overview.daily": "Answers per day",
  "overview.agents": "Agents",
  "overview.people": "People and apps",
  "overview.checks": "Check history",
  "processes.title": "Business processes",
  "processes.lead":
    "Each process with the systems that feed it, the metrics and rules it uses and who owns each part.",
  "processes.empty": "The model declares no processes yet.",
  "processes.systems": "Systems",
  "processes.uses": "metrics · rules",
  "processes.failing": "failing parts",
  "processes.map": "Process map",
  "processes.legend": "Colors show health; click a box to see its owner, definition and status.",
  "processes.whole": "Whole model",
  "catalog.title": "Catalog",
  "catalog.lead":
    "Every metric, measure, business rule and glossary concept, in business words, with its owner and status.",
  "catalog.filter": "Filter by name, owner or concept",
  "catalog.versions": "versions",
  "catalog.tests": "tests",
  "catalog.synonyms": "Synonyms",
  "catalog.clearState": "Show every state",
  "catalog.mapsTo": "Defined by",
  "member.definition": "Definition",
  "member.formula": "Formula",
  "member.status": "Status",
  "member.history": "Versions",
  "member.effective": "Effective from",
  "member.current": "Current",
  "member.impact": "What depends on it",
  "member.noImpact": "Nothing else depends on it.",
  "member.owners": "Owners affected",
  "member.sources": "Source systems",
  "member.consumers": "Who used it",
  "member.reconciliations": "Reconciliations with legacy reports",
  "member.noReconciliations": "Never compared with a legacy report.",
  "member.recent": "Recent answers",
  "member.tests": "Rule tests",
  "member.processes": "Used by processes",
  "answers.title": "Audited answers",
  "answers.lead":
    "Every number an agent or app received, with the formula, rules, user context and fingerprints to reproduce it.",
  "answers.scopeAll": "Showing everyone's answers.",
  "answers.scopeMine": "Showing your answers only.",
  "answers.empty": "No answers recorded yet.",
  "answers.who": "Who",
  "answers.when": "When",
  "answers.what": "What was asked",
  "answers.result": "Result",
  "answers.rows": "rows",
  "answers.row": "row",
  "answers.copy": "Click to copy",
  "answers.failed": "Refused or failed",
  "answers.asOf": "Reference date",
  "answers.context": "User context (row filters)",
  "answers.rules": "Business rules applied",
  "answers.sql": "Generated SQL",
  "answers.fingerprints": "Fingerprints",
  "answers.modelChanged": "The model changed since this answer.",
  "answers.replay": "Reproduce this answer",
  "answers.replaying": "Reproducing…",
  "answers.verdict.identical": "Identical: same model, same SQL, same result.",
  "answers.verdict.model_changed": "The semantic model changed since the original answer.",
  "answers.verdict.engine_changed": "Same model, but the engine generated different SQL.",
  "answers.verdict.data_changed": "Same model and SQL; the source data changed.",
  "health.title": "Checks",
  "health.lead":
    "Each `cosmos check` run: business rule tests and reconciliations of governed metrics against legacy reports.",
  "health.empty": "No check runs recorded (COSMOS_RUNS_LOG).",
  "health.ruleTests": "Rule tests",
  "health.reconciliations": "Reconciliations",
  "health.case": "Case",
  "health.outcome": "Outcome",
  "health.causes": "Likely cause",
  "health.difference": "Difference",
  "health.outcome.pass": "As expected",
  "health.outcome.fail": "Diverges",
  "health.outcome.error": "Error",
  "health.outcome.skipped": "Skipped",
  "usage.title": "Usage and estimated cost",
  "usage.lead": "From the audit log; costs use the rates in connections.yml and are estimates.",
  "usage.forbidden": "Only console admins (COSMOS_CONSOLE_ADMINS) see usage per person.",
  "usage.byMetric": "By metric",
  "usage.byPrincipal": "By person or agent",
  "usage.bySource": "By data source",
  "usage.unused": "Metrics nobody asked for",
  "usage.cost": "Cost",
  "usage.avg": "Avg time",
  "reason.rule_tests_fail": "{failed} of {total} rule tests fail",
  "reason.rule_tests_pass": "{total} rule tests pass",
  "reason.no_tests": "The rule has no tests",
  "reason.never_reconciled": "Never compared with a legacy report",
  "reason.reconciliation_failed": "Diverges from “{title}” ({case})",
  "reason.reconciled": "Matches {count} legacy report(s)",
  "reason.depends_on_failing": "Depends on {member}, which is failing",
  "cause.filter_difference": "Different filter",
  "cause.different_metric": "Different definition",
  "cause.fan_out": "Rows multiplied by a join",
  "cause.time_shift": "Day cut in another time zone",
  "cause.truncation": "Integer division",
  "cause.inner_join_drop": "Rows lost in an inner join",
  "cause.count_not_distinct": "Counted rows, not distinct",
  "cause.null_group_dropped": "Blank group dropped",
  "cause.rounding": "Rounding",
  "cause.unexplained": "Unexplained",
};

type Key = keyof typeof en;

const pt: Record<Key, string> = {
  "app.title": "Cosmos Semantic",
  "app.subtitle": "Console",
  "nav.overview": "Visão geral",
  "nav.processes": "Processos",
  "nav.catalog": "Catálogo",
  "nav.answers": "Respostas",
  "nav.health": "Verificações",
  "nav.usage": "Uso e custo",
  "lang.switch": "English",
  "auth.title": "Entrar no console",
  "auth.help":
    "Cole uma chave de API ou um token de acesso do seu provedor de identidade. Ele fica só neste navegador.",
  "auth.placeholder": "Chave de API ou token",
  "auth.save": "Conectar",
  "auth.cancel": "Cancelar",
  "auth.change": "Trocar credencial",
  "auth.signout": "Sair",
  "common.loading": "Carregando…",
  "common.error": "Não foi possível carregar",
  "common.retry": "Tentar de novo",
  "common.owner": "Dono",
  "common.noOwner": "Sem dono",
  "common.description": "Descrição",
  "common.none": "Nenhum",
  "common.search": "Buscar",
  "common.all": "Todos",
  "common.queries": "consultas",
  "common.query": "consulta",
  "overview.measures": "+ {n} medidas",
  "catalog.version": "versão",
  "catalog.test": "teste",
  "common.back": "Voltar",
  "common.open": "Abrir",
  "common.since": "Últimos 30 dias",
  "state.healthy": "Saudável",
  "state.attention": "Requer atenção",
  "state.failing": "Com falha",
  "state.unchecked": "Não verificado",
  "kind.metric": "Métrica",
  "kind.measure": "Medida",
  "kind.rule": "Regra de negócio",
  "kind.concept": "Conceito",
  "kind.process": "Processo",
  "kind.system": "Sistema",
  "kind.team": "Equipe",
  "kind.model": "Modelo de dados",
  "kind.dimension": "Dimensão",
  "overview.title": "Dá para confiar nos números?",
  "overview.lead":
    "As métricas governadas, as regras de negócio por trás delas, se ainda batem com os relatórios legados e como os agentes de IA as usam.",
  "overview.metrics": "Métricas governadas",
  "overview.rules": "Regras de negócio",
  "overview.concepts": "Conceitos do glossário",
  "overview.processes": "Processos de negócio",
  "overview.ownership": "Donos e testes",
  "overview.withOwner": "Métricas com dono",
  "overview.described": "Métricas com descrição",
  "overview.rulesTested": "Regras com testes",
  "overview.ruleTests": "Testes de regra passando",
  "overview.metricHealth": "Saúde das métricas",
  "overview.lastCheck": "Última verificação",
  "overview.noCheck":
    "Nenhuma verificação rodou ainda. Agende o `cosmos check` (CI ou cron) para registrar testes de regra e reconciliações.",
  "overview.checkOk": "Tudo o que foi verificado passou",
  "overview.checkFail": "Algo verificado falhou",
  "overview.usage": "Perguntas respondidas",
  "overview.noAudit":
    "Nenhum log de auditoria está configurado (COSMOS_AUDIT_LOG), então o uso não aparece.",
  "overview.answers": "Respostas",
  "overview.byAgents": "por agentes de IA",
  "overview.errors": "erros ou recusas",
  "overview.latency": "Tempo típico",
  "overview.topMetrics": "Métricas mais consultadas",
  "overview.daily": "Respostas por dia",
  "overview.agents": "Agentes",
  "overview.people": "Pessoas e aplicações",
  "overview.checks": "Histórico de verificações",
  "processes.title": "Processos de negócio",
  "processes.lead":
    "Cada processo com os sistemas que o alimentam, as métricas e regras que usa e quem é dono de cada parte.",
  "processes.empty": "O modelo ainda não declara processos.",
  "processes.systems": "Sistemas",
  "processes.uses": "métricas · regras",
  "processes.failing": "partes com falha",
  "processes.map": "Mapa do processo",
  "processes.legend": "As cores mostram a saúde; clique numa caixa para ver dono, definição e status.",
  "processes.whole": "Modelo inteiro",
  "catalog.title": "Catálogo",
  "catalog.lead":
    "Todas as métricas, medidas, regras de negócio e conceitos do glossário, em linguagem de negócio, com dono e status.",
  "catalog.filter": "Filtrar por nome, dono ou conceito",
  "catalog.versions": "versões",
  "catalog.tests": "testes",
  "catalog.synonyms": "Sinônimos",
  "catalog.clearState": "Mostrar todos os estados",
  "catalog.mapsTo": "Definido por",
  "member.definition": "Definição",
  "member.formula": "Fórmula",
  "member.status": "Status",
  "member.history": "Versões",
  "member.effective": "Vigente desde",
  "member.current": "Atual",
  "member.impact": "O que depende dela",
  "member.noImpact": "Nada mais depende dela.",
  "member.owners": "Donos afetados",
  "member.sources": "Sistemas de origem",
  "member.consumers": "Quem usou",
  "member.reconciliations": "Reconciliações com relatórios legados",
  "member.noReconciliations": "Nunca comparada com um relatório legado.",
  "member.recent": "Respostas recentes",
  "member.tests": "Testes da regra",
  "member.processes": "Usada pelos processos",
  "answers.title": "Respostas auditadas",
  "answers.lead":
    "Cada número que um agente ou aplicação recebeu, com a fórmula, as regras, o contexto do usuário e as impressões digitais para reproduzi-lo.",
  "answers.scopeAll": "Mostrando as respostas de todos.",
  "answers.scopeMine": "Mostrando só as suas respostas.",
  "answers.empty": "Nenhuma resposta registrada ainda.",
  "answers.who": "Quem",
  "answers.when": "Quando",
  "answers.what": "O que foi perguntado",
  "answers.result": "Resultado",
  "answers.rows": "linhas",
  "answers.row": "linha",
  "answers.copy": "Clique para copiar",
  "answers.failed": "Recusada ou com erro",
  "answers.asOf": "Data de referência",
  "answers.context": "Contexto do usuário (filtros de linha)",
  "answers.rules": "Regras de negócio aplicadas",
  "answers.sql": "SQL gerado",
  "answers.fingerprints": "Impressões digitais",
  "answers.modelChanged": "O modelo mudou desde esta resposta.",
  "answers.replay": "Reproduzir esta resposta",
  "answers.replaying": "Reproduzindo…",
  "answers.verdict.identical": "Idêntica: mesmo modelo, mesmo SQL, mesmo resultado.",
  "answers.verdict.model_changed": "O modelo semântico mudou desde a resposta original.",
  "answers.verdict.engine_changed": "Mesmo modelo, mas o motor gerou outro SQL.",
  "answers.verdict.data_changed": "Mesmo modelo e mesmo SQL; os dados da fonte mudaram.",
  "health.title": "Verificações",
  "health.lead":
    "Cada execução do `cosmos check`: testes das regras de negócio e reconciliações das métricas governadas com os relatórios legados.",
  "health.empty": "Nenhuma verificação registrada (COSMOS_RUNS_LOG).",
  "health.ruleTests": "Testes de regra",
  "health.reconciliations": "Reconciliações",
  "health.case": "Caso",
  "health.outcome": "Resultado",
  "health.causes": "Causa provável",
  "health.difference": "Diferença",
  "health.outcome.pass": "Como esperado",
  "health.outcome.fail": "Diverge",
  "health.outcome.error": "Erro",
  "health.outcome.skipped": "Pulado",
  "usage.title": "Uso e custo estimado",
  "usage.lead": "A partir do log de auditoria; os custos usam as tarifas do connections.yml e são estimativas.",
  "usage.forbidden": "Só administradores do console (COSMOS_CONSOLE_ADMINS) veem o uso por pessoa.",
  "usage.byMetric": "Por métrica",
  "usage.byPrincipal": "Por pessoa ou agente",
  "usage.bySource": "Por fonte de dados",
  "usage.unused": "Métricas que ninguém consultou",
  "usage.cost": "Custo",
  "usage.avg": "Tempo médio",
  "reason.rule_tests_fail": "{failed} de {total} testes da regra falham",
  "reason.rule_tests_pass": "{total} testes da regra passam",
  "reason.no_tests": "A regra não tem testes",
  "reason.never_reconciled": "Nunca comparada com um relatório legado",
  "reason.reconciliation_failed": "Diverge de “{title}” ({case})",
  "reason.reconciled": "Bate com {count} relatório(s) legado(s)",
  "reason.depends_on_failing": "Depende de {member}, que está com falha",
  "cause.filter_difference": "Filtro diferente",
  "cause.different_metric": "Definição diferente",
  "cause.fan_out": "Linhas multiplicadas por um join",
  "cause.time_shift": "Dia cortado em outro fuso",
  "cause.truncation": "Divisão inteira",
  "cause.inner_join_drop": "Linhas perdidas num inner join",
  "cause.count_not_distinct": "Contou linhas, não distintos",
  "cause.null_group_dropped": "Grupo em branco descartado",
  "cause.rounding": "Arredondamento",
  "cause.unexplained": "Sem explicação",
};

const dictionaries: Record<Lang, Record<Key, string>> = { en, pt };

interface I18n {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (key: Key, params?: Record<string, string | number>) => string;
  count: (n: number | null | undefined, one: Key, many: Key) => string;
  reason: (reason: Reason) => string;
  state: (state: State | undefined) => string;
  label: (labels: Labels | undefined, fallback: string) => string;
  number: (value: number | null | undefined, digits?: number) => string;
  date: (iso: string | null | undefined) => string;
}

const Context = createContext<I18n | null>(null);
const STORAGE = "cosmos.lang";

function format(text: string, params?: Record<string, string | number>): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? `{${name}}`));
}

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>("en");

  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE);
      if (saved === "en" || saved === "pt") setLangState(saved);
    } catch {
      /* storage unavailable: English */
    }
  }, []);

  useEffect(() => {
    document.documentElement.lang = lang === "pt" ? "pt-BR" : "en";
  }, [lang]);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem(STORAGE, next);
    } catch {
      /* not remembered */
    }
  }, []);

  const value = useMemo<I18n>(() => {
    const dict = dictionaries[lang];
    const locale = lang === "pt" ? "pt-BR" : "en-US";
    const t = (key: Key, params?: Record<string, string | number>) =>
      format(dict[key] ?? en[key] ?? key, params);
    return {
      lang,
      setLang,
      t,
      count: (n, one, many) =>
        `${(n ?? 0).toLocaleString(locale)} ${t(n === 1 ? one : many)}`,
      reason: (r) => t(`reason.${r.code}` as Key, r as unknown as Record<string, string | number>),
      state: (s) => t(`state.${s ?? "unchecked"}` as Key),
      label: (labels, fallback) => labels?.[lang] ?? labels?.en ?? fallback,
      number: (v, digits = 0) =>
        v === null || v === undefined
          ? "—"
          : v.toLocaleString(locale, { maximumFractionDigits: digits }),
      date: (iso) =>
        iso
          ? new Date(iso).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" })
          : "—",
    };
  }, [lang, setLang]);

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useI18n(): I18n {
  const value = useContext(Context);
  if (!value) throw new Error("useI18n outside I18nProvider");
  return value;
}

export function causeKey(code: string): Key {
  return (`cause.${code}` in en ? `cause.${code}` : "cause.unexplained") as Key;
}

export type { Key };
