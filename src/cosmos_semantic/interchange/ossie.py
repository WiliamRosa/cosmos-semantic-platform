"""Importing and exporting Apache Ossie (formerly Open Semantic Interchange), 0.1.x and 0.2.

Import:

- ``datasets`` become models; ``fields`` with ``dimension`` become dimensions.
- ``relationships`` become entities (primary on the target, foreign on the source).
- Metrics: simple aggregations become measures; compound expressions become one measure per
  aggregation plus a derived metric, across datasets too.
- ``ai_context.synonyms`` becomes glossary entries, so agents find the member by synonym.
- Cosmos's own ``custom_extensions`` restore the model without loss; other vendors'
  extensions are kept in ``custom_extensions``.

Export (models, entities, dimensions, measures, metrics, row filters, sensitivity, the
glossary, business rules and business context):

- Models become ``datasets``, dimensions and keys become ``fields``, entities become
  ``primary_key``, ``unique_keys`` and explicit ``relationships``.
- Measures and metrics that aggregate one dataset become ``metrics`` on that dataset, in
  ``OSSIE_SQL_2026``, written as the planner computes them: a filter becomes ``CASE WHEN``
  inside the aggregation, AVG runs in double precision and division is real and null on a zero
  denominator.
- The exported part of the project goes, whole, into a ``COSMOS_SEMANTIC`` extension, which
  ``import_ossie`` restores without loss. Metrics spanning datasets (a naive reader could fan
  them out), cumulative ones and filters Ossie cannot express live only there, with a warning.
- Row filters (with the ``user_context`` fields they read) and sensitivity have no native
  Ossie field: they are in the root extension and, for other tools, in a ``COSMOS_SEMANTIC``
  extension on their dataset and field, which ``import_ossie`` also reads when the root
  extension is gone. The glossary is in the root extension, and each concept's labels and
  definition become ``ai_context`` (``synonyms``, ``instructions``) of the member it maps to.
- Business rules (every version in ``history``, with owner, effective dates and tests), teams,
  systems and processes have no native Ossie field: they are in the root extension, so a row
  filter that reads a rule and a glossary concept that maps to one are exported too. The rules
  a dataset's row filters read (with the rules those bind to) are also copied into that
  dataset's extension, with the business time zone, so the fallback import still has them. A metric whose filter reads a rule lives only in the root extension.
- User context fields that nothing exported reads are not exported; each one becomes a warning,
  and the extension records how many were omitted, which ``import_ossie`` reports again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from cosmos_semantic.compiler import SemanticGraph, compile_project
from cosmos_semantic.errors import QueryError
from cosmos_semantic.expressions import REF_RE, USER_RE
from cosmos_semantic.interchange import ExportResult, ImportResult, read_yaml_files
from cosmos_semantic.spec import (
    DerivedMetric,
    Project,
    RatioMetric,
    SimpleMetric,
)

VENDOR = "COSMOS_SEMANTIC"
OSSIE_VERSION = "0.2.0.dev0"
_NATIVE = "OSSIE_SQL_2026"
_DIALECT_ORDER = ("OSSIE_SQL_2026", "ANSI_SQL", "DATABRICKS", "SNOWFLAKE", "BIGQUERY")
_AGGS = {exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max", exp.Count: "count"}
_TYPE = {
    "Boolean": "boolean",
    "Integer": "number",
    "Decimal": "number",
    "Float": "number",
    "Date": "time",
    "Time": "time",
    "DateTime": "time",
    "DateTimeTz": "time",
}


def _expression(obj: dict | None, where: str, warnings: list[str]) -> str | None:
    if not obj:
        return None
    dialects = {d.get("dialect"): d.get("expression") for d in obj.get("dialects") or []}
    for name in _DIALECT_ORDER:
        if dialects.get(name):
            if name not in ("OSSIE_SQL_2026", "ANSI_SQL"):
                warnings.append(f"{where}: expression only in dialect {name}; review portability")
            return dialects[name]
    if dialects:
        name, text = next(iter(dialects.items()))
        warnings.append(f"{where}: dialect {name} is opaque in Ossie; expression used as is")
        return text
    return None


def _ident(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_") or "x"


def import_ossie(path: str | Path) -> ImportResult:
    files = read_yaml_files(path)
    if len(files) != 1:
        raise ValueError("pass a single Ossie document (YAML or JSON)")
    _, text = files[0]
    doc = yaml.safe_load(text)
    if isinstance(doc.get("semantic_model"), list):
        doc = doc["semantic_model"][0]
    warnings: list[str] = []

    root = _cosmos_data(doc, "custom_extensions")
    if "project" in root:
        warnings += _omitted_warnings(root.get("omitted") or {})
        return ImportResult(Project.model_validate(root["project"]), warnings)
    if any(e.get("vendor_name") == VENDOR for e in doc.get("custom_extensions") or []):
        warnings.append(
            f"custom_extensions: the {VENDOR} extension at the root has no project; the model "
            "is read from the native part of the document"
        )
    copies: list[str] = []

    datasets = {d["name"]: d for d in doc.get("datasets") or []}
    model_names = {name: _ident(name) for name in datasets}
    field_exprs: dict[str, dict[str, str]] = {}
    models: dict[str, dict[str, Any]] = {}
    glossary: list[dict[str, Any]] = []
    user_context: dict[str, Any] = {}
    rules: dict[str, Any] = {}
    zones: dict[str, str] = {}

    for name, ds in datasets.items():
        mname = model_names[name]
        where = f"datasets.{name}"
        model: dict[str, Any] = {"name": mname, "table": ds["source"]}
        if ds.get("description"):
            model["description"] = ds["description"]
        exprs: dict[str, str] = {}
        policies = _cosmos_data(ds, f"{where}.custom_extensions")
        if policies.get("row_filters"):
            model["row_filters"] = policies["row_filters"]
            copies += [f"{mname}.{f['name']}" for f in policies["row_filters"]]
            _merge_user_context(user_context, policies.get("user_context") or {}, where)
            _merge_rules(rules, policies.get("business_rules") or [], where)
            _merge_timezone(zones, policies, where)
        for fld in ds.get("fields") or []:
            fexpr = _expression(fld.get("expression"), f"{where}.fields.{fld['name']}", warnings)
            exprs[fld["name"]] = fexpr or fld["name"]
            if "dimension" not in fld:
                continue
            dim_name = _ident(fld["name"])
            is_time = (fld.get("dimension") or {}).get("is_time")
            kind = "time" if is_time else _TYPE.get(fld.get("datatype"), "categorical")
            if is_time is False and kind == "time":
                kind = "categorical"
            item = {"name": dim_name, "type": kind}
            if fexpr and fexpr != dim_name:
                item["expr"] = fexpr
            if fld.get("description"):
                item["description"] = fld["description"]
            marking = _cosmos_data(fld, f"{where}.fields.{fld['name']}.custom_extensions")
            if marking.get("sensitivity"):
                item["sensitivity"] = marking["sensitivity"]
                copies.append(f"{mname}.{dim_name}")
            model.setdefault("dimensions", []).append(item)
            _synonyms(glossary, fld, f"dimension:{mname}.{dim_name}", f"{mname}_{dim_name}")
        field_exprs[name] = exprs
        if ds.get("primary_key"):
            _key(model, mname, ds["primary_key"], exprs, "primary", where, warnings)
        for key in ds.get("unique_keys") or []:
            _key(model, f"{mname}_{'_'.join(key)}", key, exprs, "unique", where, warnings)
        models[name] = model

    for rel in doc.get("relationships") or []:
        source, target = rel["from"], rel["to"]
        where = f"relationships.{rel.get('name')}"
        if source not in models or target not in models:
            warnings.append(f"{where}: unknown dataset; skipped")
            continue
        if len(rel["from_columns"]) != 1:
            warnings.append(f"{where}: composite keys are not supported; relationship skipped")
            continue
        to_col, from_col = rel["to_columns"][0], rel["from_columns"][0]
        target_model = models[target]
        entity = next(
            (
                e
                for e in target_model.get("entities", [])
                if e["type"] in ("primary", "unique")
                and e.get("expr", e["name"]) == field_exprs[target].get(to_col, to_col)
            ),
            None,
        )
        if entity is None:
            entity = {
                "name": f"{model_names[target]}_{_ident(to_col)}",
                "type": "unique",
                "expr": field_exprs[target].get(to_col, to_col),
            }
            target_model.setdefault("entities", []).append(entity)
        models[source].setdefault("entities", []).append(
            {
                "name": entity["name"],
                "type": "foreign",
                "expr": field_exprs[source].get(from_col, from_col),
            }
        )

    metrics: list[dict[str, Any]] = []
    for name, ds in datasets.items():
        for metric in ds.get("metrics") or []:
            _metric(
                metric, models, model_names, field_exprs, metrics, glossary, warnings, scope=name
            )
    for metric in doc.get("metrics") or []:
        _metric(metric, models, model_names, field_exprs, metrics, glossary, warnings, scope=None)

    if copies:
        read_rules = f", with the business rules they read ({', '.join(rules)})," if rules else ""
        warnings.append(
            f"the row filters and sensitivity of {', '.join(copies)}{read_rules} were read from "
            f"the {VENDOR} copies on datasets and fields, not from the root {VENDOR} extension, "
            "which is missing; verify them before use. Members kept only in the root extension "
            "(metrics that span datasets, cumulative ones, metrics whose filter reads $user or a "
            "business rule, the user context only those metrics read, the glossary beyond "
            "ai_context synonyms, business rules no row filter reads, teams, systems and "
            "processes) are missing"
        )
    extensions = {
        ext.get("vendor_name", "unknown"): ext.get("data")
        for ext in doc.get("custom_extensions") or []
    }
    project = Project.model_validate(
        {
            "version": 1,
            "semantic_models": list(models.values()),
            "metrics": metrics,
            "glossary": glossary,
            "user_context": user_context,
            "business_rules": list(rules.values()),
            "timezone": zones.get("timezone"),
            "custom_extensions": {"ossie": extensions} if extensions else {},
        }
    )
    return ImportResult(project, warnings)


def _cosmos_data(member: dict, where: str) -> dict[str, Any]:
    """The Cosmos extension of the document, a dataset or a field."""
    for ext in member.get("custom_extensions") or []:
        if ext.get("vendor_name") == VENDOR:
            data = ext.get("data")
            try:
                data = json.loads(data) if isinstance(data, str) else data
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{where}: the {VENDOR} extension is not valid JSON ({exc})"
                ) from None
            if data is not None and not isinstance(data, dict):
                raise ValueError(f"{where}: the {VENDOR} extension is not a JSON object")
            return data or {}
    return {}


def _merge_user_context(merged: dict[str, Any], fields: dict[str, Any], where: str) -> None:
    """The user context the row filters of each dataset read; a field declared in two ways
    is an error, since either one would change who sees which rows."""
    for name, field in fields.items():
        if name in merged:
            old, new = merged[name], field
            if (old.get("claim"), old.get("required", True)) != (
                new.get("claim"),
                new.get("required", True),
            ):
                raise ValueError(
                    f"{where}: user_context.{name} is declared in conflicting ways in the "
                    f"{VENDOR} copies (claim {old.get('claim')!r}, required "
                    f"{old.get('required', True)} against claim {new.get('claim')!r}, required "
                    f"{new.get('required', True)}); fix the document"
                )
            continue
        merged[name] = field


def _merge_rules(merged: dict[str, Any], rules: list[dict[str, Any]], where: str) -> None:
    """The business rules the row filters of each dataset read; one rule copied with two
    definitions is an error, since either one would change who sees which rows."""
    for rule in rules:
        if merged.setdefault(rule["name"], rule) != rule:
            raise ValueError(
                f"{where}: business rule `{rule['name']}` is defined in conflicting ways in the "
                f"{VENDOR} copies; fix the document"
            )


def _merge_timezone(merged: dict[str, str], copy: dict[str, Any], where: str) -> None:
    """The business time zone the copies were written with; two different ones are an error,
    since "today" decides which rows a row filter and which rule version a query uses."""
    zone = copy.get("timezone")
    if zone is None:
        return
    if merged.setdefault("timezone", zone) != zone:
        raise ValueError(
            f"{where}: the {VENDOR} copies declare conflicting time zones "
            f"({merged['timezone']!r} and {zone!r}); fix the document"
        )


def _omitted_warnings(omitted: dict[str, Any]) -> list[str]:
    """What the exporter left out of the document, as recorded in its marker."""
    out = []
    if omitted.get("row_filters"):
        out.append(
            "row_filters: the export omitted "
            + ", ".join(omitted["row_filters"])
            + "; this model does not have that row-level security"
        )
    if omitted.get("sensitivity"):
        out.append(
            "sensitivity: the export omitted the personal-data marking of "
            + ", ".join(omitted["sensitivity"])
            + "; mark these dimensions again before agents use them"
        )
    for key, count in omitted.items():
        if key not in ("row_filters", "sensitivity") and count:
            out.append(f"{key}: the export omitted {count} item(s)")
    return out


def _key(model, entity_name, columns, exprs, kind, where, warnings) -> None:
    if len(columns) != 1:
        warnings.append(f"{where}: composite key {columns} is not supported; skipped")
        return
    model.setdefault("entities", []).append(
        {"name": entity_name, "type": kind, "expr": exprs.get(columns[0], columns[0])}
    )


def _synonyms(glossary: list, member: dict, target: str, concept: str) -> None:
    ai = member.get("ai_context") or {}
    synonyms = ai.get("synonyms") if isinstance(ai, dict) else None
    if not synonyms:
        return
    glossary.append(
        {
            "concept": concept,
            "pref_label": {"pt": member.get("label") or member["name"]},
            "alt_labels": {"pt": list(synonyms)},
            "definition": member.get("description")
            or (ai.get("instructions") if isinstance(ai, dict) else None),
            "maps_to": target,
        }
    )


def _metric(metric, models, model_names, field_exprs, metrics, glossary, warnings, scope) -> None:
    name = _ident(metric["name"])
    where = f"metrics.{metric['name']}"
    text = _expression(metric.get("expression"), where, warnings)
    if not text:
        warnings.append(f"{where}: no expression; skipped")
        return
    try:
        tree = sqlglot.parse_one(text)
    except sqlglot.errors.ParseError as exc:
        warnings.append(f"{where}: invalid expression ({exc}); skipped")
        return

    def dataset_of(column: exp.Column) -> str | None:
        if column.table:
            return column.table if column.table in models else None
        return scope

    aggregates = [n for n in tree.find_all(*_AGGS) if not n.find_ancestor(*_AGGS)]
    if not aggregates:
        warnings.append(f"{where}: the expression has no aggregation; skipped")
        return
    simple = len(aggregates) == 1 and tree is aggregates[0]
    replacements: dict[int, str] = {}
    for i, agg in enumerate(aggregates):
        columns = list(agg.find_all(exp.Column))
        owners = {dataset_of(c) for c in columns} or {scope}
        if None in owners or len(owners) != 1:
            warnings.append(
                f"{where}: aggregation `{agg.sql()}` has no single dataset; metric skipped"
            )
            return
        owner = owners.pop()
        exprs = field_exprs[owner]
        inner = agg.this
        kind = _AGGS[type(agg)]
        if isinstance(inner, exp.Distinct):
            kind, inner = "count_distinct", inner.expressions[0]

        def local(node, exprs=exprs):
            if isinstance(node, exp.Column):
                return sqlglot.parse_one(exprs.get(node.name, node.name))
            return node

        measure: dict[str, Any] = {"name": name if simple else f"{name}__m{i + 1}", "agg": kind}
        if not isinstance(inner, exp.Star):
            measure["expr"] = inner.transform(local).sql()
        if simple and metric.get("description"):
            measure["description"] = metric["description"]
        models[owner].setdefault("measures", []).append(measure)
        replacements[id(agg)] = measure["name"]

    if simple:
        _synonyms(glossary, metric, f"metric:{name}", name)
        return
    derived = tree.transform(
        lambda n: exp.column(f"__ref_{replacements[id(n)]}") if id(n) in replacements else n,
        copy=False,
    ).sql()
    for measure_name in replacements.values():
        derived = derived.replace(f"__ref_{measure_name}", "{" + measure_name + "}")
    item = {"name": name, "type": "derived", "expr": derived}
    if metric.get("description"):
        item["description"] = metric["description"]
    metrics.append(item)
    _synonyms(glossary, metric, f"metric:{name}", name)


# -- export -----------------------------------------------------------------------------------

_AGG_NODES = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}
_SPANS = (
    "it spans datasets; Ossie does not require a reader to aggregate each dataset at its own "
    "grain, so a native expression could fan out"
)


def export_ossie(
    project: Project,
    *,
    name: str = "semantic_model",
    fmt: str = "yaml",
    allow_policy_loss: bool = False,
) -> ExportResult:
    """Writes the project as an Ossie document (YAML or JSON), with a warning for each member
    that is not exported (user context fields nothing exported reads) and for each metric that
    lives only in the Cosmos extension.

    ``allow_policy_loss`` is no longer needed, since every row filter is exported; it is kept
    for compatibility and does nothing."""
    del allow_policy_loss  # deprecated, no effect
    if fmt not in ("yaml", "json"):
        raise ValueError(f"unknown format: {fmt} (use yaml or json)")
    compile_project(project)  # only valid models are exported
    warnings: list[str] = []
    exported, omitted = _exported_part(project, warnings)
    graph = compile_project(exported, strict=False)
    for issue in graph.issues:
        if issue.severity == "error":
            warnings.append(f"the exported part is not a valid model on its own: {issue}")
    writer = _OssieWriter(graph, warnings)
    snapshot = exported.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    data: dict[str, Any] = {"project": snapshot}
    if omitted:
        data["omitted"] = omitted
    extensions = [
        {"vendor_name": vendor, "data": value if isinstance(value, str) else json.dumps(value)}
        for vendor, value in (project.custom_extensions.get("ossie") or {}).items()
        if vendor != VENDOR
    ]
    extensions.append({"vendor_name": VENDOR, "data": json.dumps(data, ensure_ascii=False)})
    doc = {
        "version": OSSIE_VERSION,
        "name": name,
        "datasets": writer.datasets(),
        "relationships": writer.relationships(),
        "metrics": [],  # metrics spanning datasets stay in the extension (see _SPANS)
        "custom_extensions": extensions,
    }
    if fmt == "json":
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    else:
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    return ExportResult(text, list(dict.fromkeys(warnings)))


def _exported_part(project: Project, warnings: list[str]) -> tuple[Project, dict[str, Any]]:
    """The project without the user context fields nothing exported reads, with a warning for
    them and the marker of what was omitted: counts only, never values. Everything else,
    business rules and business context included, is exported."""
    data = project.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    omitted: dict[str, Any] = {}
    user_fields: set[str] = set()
    for model in data.get("semantic_models", []):
        for row_filter in model.get("row_filters", []):
            user_fields.update(USER_RE.findall(row_filter["filter"]))
    # Metric filters may read the user too; those metrics are kept in the extension.
    user_fields.update(USER_RE.findall(json.dumps(data.get("metrics", []))))
    context = data.get("user_context") or {}
    rest = {k: v for k, v in context.items() if k not in user_fields}
    data["user_context"] = {k: v for k, v in context.items() if k in user_fields}
    if rest:
        omitted["user_context"] = len(rest)
        warnings.append(
            f"user_context: {', '.join(rest)} not exported to Ossie yet (no exported row "
            "filter or metric uses them)"
        )
    return Project.model_validate(data), omitted


def _rules_read(graph: SemanticGraph, model: str, texts: list[str]) -> list[str]:
    """The business rules row filters read, directly or through a rule input bound to another
    rule, in the order of ``business_rules``."""
    own = {d.name for d in graph.models[model].spec.dimensions}
    todo = []
    for text in texts:
        for ref in REF_RE.findall(text):
            dim = graph.resolve_dimension(ref, context=model)
            if dim.name not in own and dim.name in graph.rules:
                todo.append(dim.name)
    found: set[str] = set()
    while todo:
        name = todo.pop()
        if name in found:
            continue
        found.add(name)
        for binding in graph.rules[name].applies_to:
            spec = {d.name for d in graph.models[binding.model].spec.dimensions}
            todo += [t for t in binding.bind.values() if t in graph.rules and t not in spec]
    return [r.name for r in graph.project.business_rules if r.name in found]


def _extension(data: dict[str, Any]) -> list[dict[str, str]]:
    return [{"vendor_name": VENDOR, "data": json.dumps(data, ensure_ascii=False)}]


def _dialects(text: str) -> dict[str, Any]:
    return {"dialects": [{"dialect": _NATIVE, "expression": text}]}


def _real_division(node: exp.Expression) -> exp.Expression:
    """Division as the planner computes it in metrics: real, and null on a zero denominator."""
    if isinstance(node, exp.Div):
        return exp.Div(
            this=exp.Cast(this=node.this, to=exp.DataType.build("double")),
            expression=exp.Nullif(this=node.expression, expression=exp.Literal.number(0)),
        )
    return node


def _both(a: str | None, b: str | None) -> str | None:
    if a and b:
        return f"({a}) AND ({b})"
    return a or b


class _NotNative(Exception):
    """A metric Ossie cannot express; the message is the reason."""


class _OssieWriter:
    """Builds the native part of the document from a compiled graph."""

    def __init__(self, graph: SemanticGraph, warnings: list[str]) -> None:
        self.graph = graph
        self.warnings = warnings
        self.fields: dict[str, list[dict[str, Any]]] = {}
        self.by_expr: dict[str, dict[str, str]] = {}
        self.field_exprs: dict[str, dict[str, str]] = {}
        self.dataset_metrics: dict[str, list[dict[str, Any]]] = {}
        for node in graph.models.values():
            self._fields(node.name)
        for node in graph.models.values():
            for measure in node.spec.measures:
                self._place(measure.name, measure.description, None)
        for metric in graph.project.metrics:
            self._place(metric.name, metric.description, metric.label)

    # -- datasets ---------------------------------------------------------------------------

    def _fields(self, model: str) -> None:
        fields: list[dict[str, Any]] = []
        self.fields[model] = fields
        self.by_expr[model] = {}
        self.field_exprs[model] = {}
        for dim in self.graph.models[model].spec.dimensions:
            text = dim.expr or dim.name
            item: dict[str, Any] = {"name": dim.name}
            if dim.type == "boolean":
                item["datatype"] = "Boolean"
            item["dimension"] = {"is_time": True} if dim.type == "time" else {}
            item["expression"] = _dialects(text)
            if dim.description:
                item["description"] = dim.description
            if dim.sensitivity:
                # No native Ossie field; also in the root extension, the source of truth.
                item["custom_extensions"] = _extension({"sensitivity": dim.sensitivity})
            fields.append(item)
            self.by_expr[model].setdefault(text, dim.name)
            self.field_exprs[model][dim.name] = text

    def _key_field(self, model: str, entity_name: str) -> str:
        """The field holding an entity's key expression, added when no field holds it yet."""
        entity = self.graph.models[model].entities[entity_name]
        text = entity.expr or entity.name
        if text in self.by_expr[model]:
            return self.by_expr[model][text]
        base = text if text.isidentifier() else f"{entity.name}_key"
        name, n = base, 2
        while name in self.field_exprs[model]:
            name, n = f"{base}_{n}", n + 1
        self.fields[model].append({"name": name, "expression": _dialects(text)})
        self.by_expr[model][text] = name
        self.field_exprs[model][name] = text
        return name

    def datasets(self) -> list[dict[str, Any]]:
        out = []
        for node in self.graph.models.values():
            spec = node.spec
            keys = {e.name: self._key_field(spec.name, e.name) for e in spec.entities}
            item: dict[str, Any] = {"name": spec.name, "source": spec.table}
            primary = [keys[e.name] for e in spec.entities if e.type == "primary"]
            if primary:
                item["primary_key"] = primary[:1]
            unique = [[keys[e.name]] for e in spec.entities if e.type == "unique"]
            unique += [[k] for k in primary[1:]]
            if unique:
                item["unique_keys"] = unique
            if spec.description:
                item["description"] = spec.description
            item["fields"] = self.fields[spec.name]
            if self.dataset_metrics.get(spec.name):
                item["metrics"] = self.dataset_metrics[spec.name]
            if spec.row_filters:
                # No native Ossie field; also in the root extension, the source of truth.
                filters = [f.model_dump(mode="json") for f in spec.row_filters]
                context = self.graph.project.user_context
                used = sorted({u for f in spec.row_filters for u in USER_RE.findall(f.filter)})
                copy: dict[str, Any] = {
                    "row_filters": filters,
                    "user_context": {
                        u: context[u].model_dump(mode="json", exclude_none=True) for u in used
                    },
                }
                if self.graph.project.timezone:
                    # "Today" for the rules and CURRENT_DATE in these row filters.
                    copy["timezone"] = self.graph.project.timezone
                read = _rules_read(self.graph, spec.name, [f.filter for f in spec.row_filters])
                if read:
                    # The fallback import needs the rules these row filters read.
                    copy["business_rules"] = [
                        self.graph.rules[r].model_dump(
                            mode="json", exclude_none=True, exclude_defaults=True
                        )
                        for r in read
                    ]
                item["custom_extensions"] = _extension(copy)
            out.append(item)
        self._ai_context(out)
        return out

    def _ai_context(self, datasets: list[dict[str, Any]]) -> None:
        """The glossary's labels and definitions as ``ai_context`` of the member each concept
        maps to, where that member is in the native part (Ossie's own place for synonyms)."""
        members: dict[str, dict[str, Any]] = {}
        for ds in datasets:
            fields = {f["name"]: f for f in ds["fields"]}
            for dim in self.graph.models[ds["name"]].spec.dimensions:
                members[f"dimension:{ds['name']}.{dim.name}"] = fields[dim.name]
            for entity in self.graph.models[ds["name"]].spec.entities:
                if entity.type != "foreign":
                    key = fields[self._key_field(ds["name"], entity.name)]
                    members.setdefault(f"entity:{entity.name}", key)
            for metric in ds.get("metrics", []):
                members[f"metric:{metric['name']}"] = metric
                members[f"measure:{metric['name']}"] = metric
        for concept in self.graph.project.glossary:
            target = concept.maps_to or ""
            if target.startswith("dimension:") and "." not in target:
                suffix = "." + target.split(":", 1)[1]
                owners = [k for k in members if k.startswith("dimension:") and k.endswith(suffix)]
                target = owners[0] if len(owners) == 1 else target
            member = members.get(target)
            if member is None:
                continue
            ai = member.setdefault("ai_context", {})
            labels = [*concept.pref_label.values()]
            labels += [x for values in concept.alt_labels.values() for x in values]
            synonyms = ai.setdefault("synonyms", [])
            synonyms += [x for x in dict.fromkeys(labels) if x not in synonyms]
            if concept.definition:
                text = ai.get("instructions")
                ai["instructions"] = f"{text}\n{concept.definition}" if text else concept.definition

    def relationships(self) -> list[dict[str, Any]]:
        """Joins the engine infers from entities, written as explicit relationships."""
        out = []
        for source, edges in self.graph.edges.items():
            for edge in edges:
                out.append(
                    {
                        "name": f"{source}_{edge.entity}",
                        "from": source,
                        "to": edge.target,
                        "from_columns": [self._key_field(source, edge.entity)],
                        "to_columns": [self._key_field(edge.target, edge.entity)],
                    }
                )
        return out

    # -- metrics ----------------------------------------------------------------------------

    def _place(self, name: str, description: str | None, label: str | None) -> None:
        """Only a metric that aggregates one dataset goes into the native part, on that
        dataset; the others stay in the extension, with a warning stating why."""
        try:
            tree, owners = self._native(name, None)
            if len(owners) != 1:
                raise _NotNative(_SPANS)
        except _NotNative as exc:
            self.warnings.append(
                f"metrics.{name}: {exc}; it is kept only in the {VENDOR} extension, so other "
                "tools will not see it"
            )
            return
        item: dict[str, Any] = {"name": name}
        if label:
            item["label"] = label
        if description:
            item["description"] = description
        item["expression"] = _dialects(tree.sql())
        self.dataset_metrics.setdefault(next(iter(owners)), []).append(item)

    def _native(self, name: str, flt: str | None) -> tuple[exp.Expression, set[str]]:
        """The metric as one SQL expression over dataset columns, with the datasets it
        aggregates. Mirrors the planner: a metric's filter applies to every measure under it.
        Raises _NotNative with the reason when Ossie cannot express it."""
        g = self.graph
        if name in g.measure_owner:
            return self._aggregate(name, flt)
        metric = g.metrics[name]
        if isinstance(metric, SimpleMetric):
            return self._aggregate(metric.measure, _both(metric.filter, flt))
        if isinstance(metric, RatioMetric):
            both = _both(metric.filter, flt)
            num = self._part(metric.numerator, both)
            den = self._part(metric.denominator, both)
            tree = _real_division(exp.Div(this=num[0], expression=den[0]))
            return tree, num[1] | den[1]
        if isinstance(metric, DerivedMetric):
            refs: list[str] = []

            def mask(match):
                refs.append(match.group(1))
                return f"__ref_{len(refs) - 1}"

            tree = sqlglot.parse_one(REF_RE.sub(mask, metric.expr)).transform(_real_division)
            bare = [c.sql() for c in tree.find_all(exp.Column) if not c.name.startswith("__ref_")]
            if bare:
                raise _NotNative(
                    f"its expression uses bare columns ({', '.join(bare)}), not "
                    "{member} references"
                )
            parts = [self._part(ref, flt) for ref in refs]

            def swap(node):
                if isinstance(node, exp.Column) and node.name.startswith("__ref_"):
                    return exp.Paren(this=parts[int(node.name[6:])][0].copy())
                return node

            owners: set[str] = set()
            for part in parts:
                owners |= part[1]
            return tree.transform(swap), owners
        raise _NotNative("it is cumulative, and Ossie has no cumulative metrics")

    def _part(self, ref: str, flt: str | None) -> tuple[exp.Expression, set[str]]:
        try:
            return self._native(ref, flt)
        except _NotNative as exc:
            raise _NotNative(f"it uses `{ref}`, and {exc}") from None

    def _aggregate(self, measure_name: str, flt: str | None) -> tuple[exp.Expression, set[str]]:
        """A measure as the planner aggregates it: a filter becomes CASE WHEN inside the
        aggregation, and AVG is computed in double precision."""
        node, measure = self.graph.measure(measure_name)
        model = node.name
        inner: exp.Expression | None = None
        if measure.expr:
            inner = self._checked(sqlglot.parse_one(measure.expr), model, measure_name)
        elif measure.agg != "count":
            inner = exp.column(measure.name)
        if flt:
            condition = self._condition(flt, model)
            then = inner if inner is not None else exp.Literal.number(1)
            inner = exp.Case(ifs=[exp.If(this=condition, true=then)])
        if measure.agg == "count_distinct":
            tree: exp.Expression = exp.Count(this=exp.Distinct(expressions=[inner]))
        elif measure.agg == "count":
            tree = exp.Count(this=inner if inner is not None else exp.Star())
        elif measure.agg == "avg":
            # The planner's cast: AVG over integers truncates in SQL Server.
            tree = exp.Avg(this=exp.Cast(this=inner, to=exp.DataType.build("double")))
        else:
            tree = _AGG_NODES[measure.agg](this=inner)
        return tree, {model}

    def _checked(self, tree: exp.Expression, model: str, member: str) -> exp.Expression:
        """Ossie resolves a name to the field of that name first, so a measure column shadowed
        by a field with another expression is reported."""
        for column in tree.find_all(exp.Column):
            text = self.field_exprs[model].get(column.name)
            if text is not None and text != column.name:
                self.warnings.append(
                    f"metrics.{member}: column `{column.name}` has the name of a field whose "
                    f"expression is `{text}`; tools reading Ossie may use the field instead"
                )
        return tree

    def _condition(self, flt: str, model: str) -> exp.Expression:
        """A metric filter over the measure's own dataset; _NotNative when it is not one."""
        if USER_RE.search(flt):
            raise _NotNative("its filter uses $user, which only Cosmos resolves")
        own = {d.name for d in self.graph.models[model].spec.dimensions}

        def ref(match):
            text = match.group(1)
            try:
                dim = self.graph.resolve_dimension(text, context=model)
            except QueryError as exc:
                raise _NotNative(
                    f"its filter reference `{text}` does not resolve ({exc})"
                ) from None
            if dim.model != model:
                raise _NotNative(f"its filter uses `{text}` from another dataset ({dim.model})")
            if dim.grain:
                raise _NotNative(f"its filter uses a time grain (`{text}`)")
            if dim.name not in own:
                raise _NotNative(
                    f"its filter uses the business rule `{text}`, which only Cosmos evaluates"
                )
            return dim.name

        return sqlglot.parse_one(REF_RE.sub(ref, flt))
