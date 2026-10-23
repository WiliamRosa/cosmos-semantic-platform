"""Industry semantic packs (ADR 0009): reusable models mapped onto a database by name.

A pack is a semantic model written over logical tables and columns, plus a ``pack.yml`` with
synonyms. ``apply_pack`` reads the database catalog, maps every logical table and column to a
physical one, rewrites the model's expressions, drops what the database does not have (and
everything that depended on it) until the model validates, and runs every remaining metric on
the data source. The result is a draft for review, with a report of every decision.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
import yaml
from sqlglot import exp

from cosmos_semantic.errors import CosmosError, QueryError
from cosmos_semantic.loader import load_project
from cosmos_semantic.spec import Project

if TYPE_CHECKING:
    from cosmos_semantic.connectors import Connector

PACKS_DIR = Path(__file__).parent
_PREFIXES = ("tbl_", "tb_", "dim_", "fact_", "fct_", "stg_", "vw_", "v_", "d_", "f_", "t_")
_READ = "postgres"  # dialect of the pack's expressions


def available() -> list[dict[str, Any]]:
    out = []
    for manifest in sorted(PACKS_DIR.glob("*/pack.yml")):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        out.append(
            {"name": data["name"], "version": data["version"], "description": data["description"]}
        )
    return out


def _manifest(name: str) -> dict[str, Any]:
    path = PACKS_DIR / name / "pack.yml"
    if not path.exists():
        names = ", ".join(p["name"] for p in available())
        raise QueryError(f"unknown pack `{name}`; available: {names}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _norm(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    for prefix in _PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix) + 2:
            text = text[len(prefix) :]
            break
    return text


def _forms(name: str) -> set[str]:
    n = _norm(name)
    return {n, _singular(n)}


# -- mapping ------------------------------------------------------------------------------


@dataclass
class Mapping:
    tables: dict[str, str] = field(default_factory=dict)  # logical -> physical
    columns: dict[str, dict[str, str]] = field(default_factory=dict)  # table -> logical -> physical
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    ambiguous: list[str] = field(default_factory=list)


def map_tables(
    manifest: dict[str, Any],
    catalog: dict[str, list[str]],
    needed: dict[str, set[str]],
    overrides: dict[str, str] | None = None,
) -> Mapping:
    """Maps logical tables and their needed columns onto ``catalog`` (table -> columns)."""
    overrides = overrides or {}
    mapping = Mapping()
    by_form: dict[str, list[str]] = {}
    for physical in catalog:
        for form in _forms(physical):
            by_form.setdefault(form, []).append(physical)
    taken: set[str] = set()
    for logical, spec in manifest["tables"].items():
        if logical in overrides:
            physical = overrides[logical]
            if physical not in catalog:
                raise QueryError(f"--map {logical}={physical}: no such table")
        else:
            names = [logical, *spec.get("synonyms", [])]
            physical = None
            for name in names:  # earlier names win: the logical name, then synonyms in order
                hits = sorted({p for f in _forms(name) for p in by_form.get(f, [])} - taken)
                if len(hits) == 1:
                    physical = hits[0]
                    break
                if len(hits) > 1:
                    mapping.ambiguous.append(f"{logical}: {', '.join(hits)}")
                    break
            if physical is None:
                mapping.missing_tables.append(logical)
                continue
        taken.add(physical)
        mapping.tables[logical] = physical
        columns = {_norm(c): c for c in catalog[physical]}
        entity = _singular(logical)
        found, missing = {}, []
        for column in sorted(needed.get(logical, set())):
            key = f"{logical}.{column}"
            if key in overrides:
                if overrides[key] not in catalog[physical]:
                    raise QueryError(f"--map {key}={overrides[key]}: no such column")
                found[column] = overrides[key]
                continue
            candidates = [
                column,
                *spec.get("columns", {}).get(column, []),
                f"{entity}_{column}",
                *(["id"] if column == f"{entity}_id" else []),
            ]
            match = next((columns[_norm(c)] for c in candidates if _norm(c) in columns), None)
            if match is None:
                missing.append(column)
            else:
                found[column] = match
        mapping.columns[logical] = found
        if missing:
            mapping.missing_columns[logical] = missing
    return mapping


# -- rewriting ------------------------------------------------------------------------------


def _columns_of(sql: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, read=_READ)
    except sqlglot.errors.ParseError:
        return {sql}
    return {c.name for c in tree.find_all(exp.Column)}


def _rename(sql: str, renames: dict[str, str]) -> str:
    tree = sqlglot.parse_one(sql, read=_READ)

    def swap(node):
        if isinstance(node, exp.Column) and node.name in renames:
            return exp.column(
                renames[node.name], quoted=not re.match(r"^[a-z_][a-z0-9_]*$", renames[node.name])
            )
        return node

    return tree.transform(swap).sql(dialect=_READ)


def _needed(data: dict[str, Any]) -> dict[str, set[str]]:
    """Physical columns the pack's models read, per logical table."""
    needed: dict[str, set[str]] = {}
    for model in data["semantic_models"]:
        cols = needed.setdefault(model["name"], set())
        for kind in ("entities", "dimensions"):
            for item in model.get(kind, []):
                cols |= _columns_of(item.get("expr") or item["name"])
        for item in model.get("measures", []):
            if item.get("expr") or item["agg"] != "count":
                cols |= _columns_of(item.get("expr") or item["name"])
    return needed


@dataclass
class PackResult:
    project: Project
    report: dict[str, Any]


def apply_pack(
    name: str,
    connector: Connector,
    *,
    schema: str = "public",
    overrides: dict[str, str] | None = None,
) -> PackResult:
    from cosmos_semantic.bootstrap import Bootstrapper

    manifest = _manifest(name)
    pack = load_project(PACKS_DIR / name / "semantic")
    data = pack.model_dump(mode="json", exclude_none=True)
    catalog = {
        t.name: [c.name for c in t.columns] for t in Bootstrapper(connector, schema).read_catalog()
    }
    mapping = map_tables(manifest, catalog, _needed(data), overrides)
    removed: list[dict[str, str]] = []

    models = []
    for model in data["semantic_models"]:
        logical = model["name"]
        if logical not in mapping.tables:
            removed.append({"member": f"model:{logical}", "reason": "no matching table"})
            continue
        model["table"] = f"{schema}.{mapping.tables[logical]}"
        found = mapping.columns[logical]
        renames = {k: v for k, v in found.items() if k != v}
        for kind in ("entities", "dimensions", "measures"):
            kept = []
            for item in model.get(kind, []):
                if kind == "measures" and not item.get("expr") and item["agg"] == "count":
                    kept.append(item)
                    continue
                source = item.get("expr") or item["name"]
                lacking = sorted(_columns_of(source) - set(found))
                if lacking:
                    removed.append(
                        {
                            "member": f"{kind[:-1] if kind != 'entities' else 'entity'}:"
                            f"{logical}.{item['name']}",
                            "reason": f"no matching column for {', '.join(lacking)}",
                        }
                    )
                    continue
                if renames:
                    item["expr"] = _rename(source, renames)
                    if item["expr"] == item["name"]:
                        item.pop("expr")
                kept.append(item)
            model[kind] = kept
        models.append(model)
    data["semantic_models"] = models
    _rebind_rules(data, mapping, removed)
    data = _prune(data, removed)

    project = Project.model_validate(data)
    runs = _smoke(project, connector)
    failed = [r for r in runs if not r["ok"]]
    if failed:
        for r in failed:
            removed.append({"member": f"metric:{r['metric']}", "reason": r["error"]})
        data["metrics"] = [
            m for m in data["metrics"] if m["name"] not in {r["metric"] for r in failed}
        ]
        data = _prune(data, removed)
        project = Project.model_validate(data)

    from cosmos_semantic.ontology import run_rule_tests

    tests = run_rule_tests(project)
    report = {
        "pack": {"name": manifest["name"], "version": manifest["version"]},
        "schema": schema,
        "tables": mapping.tables,
        "columns_renamed": {
            t: {k: v for k, v in cols.items() if k != v}
            for t, cols in mapping.columns.items()
            if any(k != v for k, v in cols.items())
        },
        "missing_tables": mapping.missing_tables,
        "missing_columns": mapping.missing_columns,
        "ambiguous": mapping.ambiguous,
        "removed": removed,
        "metrics_checked": len(runs) - len(failed),
        "rule_tests": {"passed": sum(t.passed for t in tests), "total": len(tests)},
    }
    return PackResult(project, report)


def _rebind_rules(data: dict[str, Any], mapping: Mapping, removed: list[dict[str, str]]) -> None:
    """A rule input bound to a dropped dimension would silently read a column that does not
    exist; such bindings go, and a rule left without bindings goes too."""
    dims = {
        m["name"]: {d["name"] for d in m.get("dimensions", [])} for m in data["semantic_models"]
    }
    rules = []
    for rule in data.get("business_rules", []):
        bindings = []
        for binding in rule["applies_to"]:
            model = binding["model"]
            if model not in dims:
                continue
            found = mapping.columns.get(model, {})
            bind, ok = {}, True
            for name, target in binding["bind"].items():
                if target in dims[model]:
                    bind[name] = target
                elif target in found:
                    bind[name] = found[target]
                else:
                    ok = False
            if ok:
                bindings.append({**binding, "bind": bind})
            else:
                removed.append(
                    {"member": f"rule:{rule['name']}@{model}", "reason": "an input lost its column"}
                )
        if bindings:
            rules.append({**rule, "applies_to": bindings})
        else:
            removed.append(
                {"member": f"rule:{rule['name']}", "reason": "no model left to apply to"}
            )
    data["business_rules"] = rules


_LOC = re.compile(
    r"^(semantic_models|metrics|business_rules|glossary)\.([^.@\s]+)(?:\.(\w+)\.(\w+))?"
)


def _prune(data: dict[str, Any], removed: list[dict[str, str]]) -> dict[str, Any]:
    """Drops whatever no longer validates, repeatedly, recording why."""
    from cosmos_semantic.compiler import compile_project

    data = copy.deepcopy(data)
    for _ in range(50):
        graph = compile_project(Project.model_validate(data), strict=False)
        errors = [i for i in graph.issues if i.severity == "error"]
        if not errors:
            return data
        progress = False
        for issue in errors:
            match = _LOC.match(issue.location)
            if not match:
                continue
            section, name, sub, sub_name = match.groups()
            if section == "semantic_models" and sub == "row_filters":
                for model in data["semantic_models"]:
                    if model["name"] == name and model.get("row_filters"):
                        before = len(model["row_filters"])
                        model["row_filters"] = [
                            f for f in model["row_filters"] if f["name"] != sub_name
                        ]
                        if len(model["row_filters"]) < before:
                            removed.append(
                                {"member": f"row_filter:{name}.{sub_name}", "reason": issue.message}
                            )
                            progress = True
                continue
            key = {
                "metrics": "name",
                "business_rules": "name",
                "glossary": "concept",
                "semantic_models": "name",
            }[section]
            before = len(data[section])
            data[section] = [i for i in data[section] if i[key] != name]
            if len(data[section]) < before:
                kind = {
                    "metrics": "metric",
                    "business_rules": "rule",
                    "glossary": "concept",
                    "semantic_models": "model",
                }[section]
                removed.append({"member": f"{kind}:{name}", "reason": issue.message})
                progress = True
        if not progress:
            raise CosmosError(
                "the pack could not be adapted: " + "; ".join(str(e) for e in errors[:3])
            )
    raise CosmosError("the pack could not be adapted: too many dependent removals")


def _smoke(project: Project, connector: Connector) -> list[dict[str, Any]]:
    """Runs every metric once on the data source, with placeholder user context."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.planner import Planner, Principal, QueryRequest

    graph = compile_project(project)
    planner = Planner(graph, connector.dialect, connector.config.schema_map)
    user = {name: "__pack_check__" for name in project.user_context}
    out = []
    for metric in project.metrics:
        dimensions = []
        if metric.type == "cumulative":  # needs one time dimension of the measure's model
            owner = graph.measure_owner.get(metric.measure)
            times = (
                [d for d, spec in graph.models[owner].dimensions.items() if spec.type == "time"]
                if owner
                else []
            )
            dimensions = [f"{owner}.{times[0]}__month"] if times else []
        try:
            request = QueryRequest(metrics=[metric.name], dimensions=dimensions, limit=1)
            compiled = planner.plan(request, Principal(user=user))
            connector.execute(compiled.sql, max_rows=1)
            out.append({"metric": metric.name, "ok": True})
        except CosmosError as exc:
            out.append({"metric": metric.name, "ok": False, "error": str(exc)[:300]})
    return out
