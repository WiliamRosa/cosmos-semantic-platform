"""Ontologia: glossário SKOS e regras de negócio (ADR 0006).

Cada regra vira uma dimensão virtual em cada modelo de ``applies_to`` (booleana para
``predicate``, categórica para ``classification``, numérica para ``derived``). Assim ela pode
ser usada como agrupamento (``customers.cliente_ativo``) ou como filtro (``{rule.cliente_ativo}``).
"""

from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from cosmos_semantic.spec import BusinessRule, Concept, Dimension, Project

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph

_INPUT_RE = re.compile(r"\{\s*([a-z_][a-z0-9_]*)\s*\}")
_RESULT_TYPE = {"predicate": "boolean", "classification": "categorical", "derived": "number"}
_SENSITIVITY_RANK = {None: 0, "personal": 1, "sensitive": 2}
RULE_DIALECT = "postgres"


# -- regras ----------------------------------------------------------------


def rule_logic(rule: BusinessRule) -> str:
    """Lógica da regra em SQL com ``{entrada}``; tabela de decisão vira CASE WHEN."""
    if rule.decision_table is None:
        assert rule.logic is not None
        return rule.logic
    parts, default = [], "NULL"
    for row in rule.decision_table:
        if row.otherwise is not None:
            default = _quote(row.otherwise)
        else:
            parts.append(f"WHEN {row.when} THEN {_quote(row.then)}")
    return f"CASE {' '.join(parts)} ELSE {default} END"


def _quote(value: str | None) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def attach_rules(g: SemanticGraph, err, warn) -> None:
    for rule in g.project.business_rules:
        loc = f"business_rules.{rule.name}"
        if rule.name in g.rules:
            err(loc, "regra duplicada")
            continue
        g.rules[rule.name] = rule
        g.rule_models[rule.name] = []
        if not rule.owner:
            warn(loc, "sem `owner`: defina a área ou pessoa responsável pela regra")
        if not rule.effective_from:
            warn(loc, "sem `effective_from`: defina desde quando a regra vale")
        if rule.decision_table is not None and not any(
            r.otherwise is not None for r in rule.decision_table
        ):
            err(loc, "tabela de decisão sem `otherwise`")
        logic = rule_logic(rule)
        used = set(_INPUT_RE.findall(logic))
        for name in used - set(rule.inputs):
            err(loc, f"a lógica usa `{{{name}}}`, que não está em `inputs`")
        try:
            sqlglot.parse_one(_INPUT_RE.sub(lambda m: m.group(1), logic), read=RULE_DIALECT)
        except sqlglot.errors.ParseError as exc:
            err(loc, f"lógica inválida: {exc}")
            continue
        for binding in rule.applies_to:
            node = g.models.get(binding.model)
            if node is None:
                err(loc, f"`applies_to` cita o modelo inexistente `{binding.model}`")
                continue
            missing = set(rule.inputs) - set(binding.bind)
            if missing:
                err(
                    loc,
                    f"entradas sem mapeamento em `{binding.model}`: {', '.join(sorted(missing))}",
                )
                continue
            if rule.name in node.dimensions:
                err(loc, f"`{binding.model}` já tem uma dimensão chamada `{rule.name}`")
                continue
            exprs, level = {}, None
            for name, target in binding.bind.items():
                dim = node.dimensions.get(target)
                if dim is not None:
                    if _type_mismatch(rule.inputs[name], dim.type):
                        err(loc, f"`{name}` é {rule.inputs[name]}, mas `{target}` é {dim.type}")
                    exprs[name] = dim.expr or dim.name
                    if _SENSITIVITY_RANK[dim.sensitivity] > _SENSITIVITY_RANK[level]:
                        level = dim.sensitivity
                else:
                    exprs[name] = target
            expr = _INPUT_RE.sub(lambda m, e=exprs: f"({e[m.group(1)]})", logic)
            node.dimensions[rule.name] = Dimension(
                name=rule.name,
                type=_RESULT_TYPE[rule.kind],  # type: ignore[arg-type]
                expr=expr,
                description=rule.description or f"Regra de negócio `{rule.name}`",
                sensitivity=level,
            )
            g.rule_models[rule.name].append(binding.model)


def _type_mismatch(declared: str, actual: str) -> bool:
    return declared != actual and not (declared == "categorical" and actual == "boolean")


@dataclass
class RuleTestResult:
    rule: str
    index: int
    passed: bool
    detail: str


def run_rule_tests(project: Project) -> list[RuleTestResult]:
    """Executa os exemplos das regras no DuckDB, sem acessar nenhuma origem."""
    import duckdb

    con = duckdb.connect()
    results = []
    for rule in project.business_rules:
        logic = rule_logic(rule)
        for i, test in enumerate(rule.tests):
            missing = set(rule.inputs) - set(test.given)
            if missing:
                results.append(
                    RuleTestResult(
                        rule.name, i, False, f"faltam entradas: {', '.join(sorted(missing))}"
                    )
                )
                continue
            text = _INPUT_RE.sub(
                lambda m, t=test, r=rule: _literal(r.inputs[m.group(1)], t.given[m.group(1)]),
                logic,
            )
            tree = sqlglot.parse_one(text, read=RULE_DIALECT)
            if test.today:
                today = exp.cast(exp.Literal.string(test.today.isoformat()), "DATE")
                tree = tree.transform(
                    lambda n, d=today: (
                        d.copy() if isinstance(n, exp.CurrentDate | exp.CurrentTimestamp) else n
                    )
                )
            try:
                got = con.execute(f"SELECT {tree.sql(dialect='duckdb')}").fetchone()[0]
            except duckdb.Error as exc:
                results.append(RuleTestResult(rule.name, i, False, str(exc)))
                continue
            ok = _same(got, test.expect)
            results.append(
                RuleTestResult(
                    rule.name, i, ok, "" if ok else f"esperado {test.expect!r}, obtido {got!r}"
                )
            )
    con.close()
    return results


def _literal(kind: str, value: Any) -> str:
    if value is None:
        return "NULL"
    if kind == "time":
        return f"CAST('{value.isoformat() if isinstance(value, dt.date) else value}' AS DATE)"
    if kind == "boolean":
        return "TRUE" if value else "FALSE"
    if kind == "number":
        return str(float(value))
    return _quote(value)


def _same(got: Any, expected: Any) -> bool:
    if isinstance(expected, int | float) and not isinstance(expected, bool) and got is not None:
        return math.isclose(float(got), float(expected), rel_tol=1e-9, abs_tol=1e-9)
    return got == expected


# -- glossário -------------------------------------------------------------


def check_glossary(g: SemanticGraph, err, warn) -> None:
    concepts = {c.concept: c for c in g.project.glossary}
    if len(concepts) != len(g.project.glossary):
        err("glossary", "conceito duplicado")
    labels: dict[str, str] = {}
    for concept in g.project.glossary:
        loc = f"glossary.{concept.concept}"
        if concept.maps_to and not resolve_maps_to(g, concept.maps_to):
            err(loc, f"`maps_to` aponta para `{concept.maps_to}`, que não existe")
        if concept.broader and concept.broader not in concepts:
            err(loc, f"`broader` cita o conceito inexistente `{concept.broader}`")
        for related in concept.related:
            if related not in concepts:
                err(loc, f"`related` cita o conceito inexistente `{related}`")
        for label in _labels(concept):
            key = " ".join(_tokens(label))
            other = labels.get(key)
            if other and other != concept.concept:
                warn(
                    loc,
                    f"o termo `{label}` também está em `{other}`; o agente não saberá qual escolher",
                )
            labels[key] = concept.concept
    for name in concepts:
        seen, current = {name}, concepts[name].broader
        while current:
            if current in seen:
                err(f"glossary.{name}", "ciclo na hierarquia `broader`")
                break
            seen.add(current)
            current = concepts[current].broader if current in concepts else None


def resolve_maps_to(g: SemanticGraph, target: str) -> bool:
    kind, _, name = target.partition(":")
    if kind == "metric":
        return name in g.metrics or name in g.measure_owner
    if kind == "measure":
        return name in g.measure_owner
    if kind == "rule":
        return name in g.rules
    if kind == "entity":
        return any(name in n.entities for n in g.models.values())
    if kind == "dimension":
        model, _, dim = name.partition(".")
        if dim:
            return model in g.models and dim in g.models[model].dimensions
        return any(name in n.dimensions for n in g.models.values())
    return False


def _labels(concept: Concept) -> list[str]:
    out = list(concept.pref_label.values())
    for values in concept.alt_labels.values():
        out.extend(values)
    return out


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith(("res", "zes", "ses")):
        return word[:-2]
    if len(word) > 4 and word.endswith("oes"):
        return word[:-3] + "ao"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", plain)]


@dataclass
class ConceptHit:
    concept: str
    label: str
    maps_to: str | None
    definition: str | None
    score: float


class Glossary:
    def __init__(self, project: Project) -> None:
        self.concepts = {c.concept: c for c in project.glossary}

    def search(self, text: str, limit: int = 5) -> list[ConceptHit]:
        query = _tokens(text)
        if not query:
            return []
        qset = set(query)
        best: dict[str, ConceptHit] = {}
        for concept in self.concepts.values():
            for label in [concept.concept.replace("_", " "), *_labels(concept)]:
                tokens = _tokens(label)
                if not tokens:
                    continue
                lset = set(tokens)
                if tokens == query:
                    score = 3.0
                elif lset <= qset:
                    score = 1.0 + len(lset) / len(qset)
                else:
                    overlap = len(lset & qset)
                    score = 0.5 * overlap / len(lset) if overlap else 0.0
                if score > 0.25 and (
                    concept.concept not in best or score > best[concept.concept].score
                ):
                    best[concept.concept] = ConceptHit(
                        concept.concept, label, concept.maps_to, concept.definition, score
                    )
        return sorted(best.values(), key=lambda h: (-h.score, h.concept))[:limit]

    def for_target(self, target: str) -> list[Concept]:
        return [c for c in self.concepts.values() if c.maps_to == target]
