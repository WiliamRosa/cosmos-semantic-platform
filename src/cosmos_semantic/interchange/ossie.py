"""Importing and exporting Apache Ossie (formerly Open Semantic Interchange), 0.1.x and 0.2.

Import:

- ``datasets`` become models; ``fields`` with ``dimension`` become dimensions.
- ``relationships`` become entities (primary on the target, foreign on the source).
- Metrics: simple aggregations become measures; compound expressions become one measure per
  aggregation plus a derived metric, across datasets too.
- ``ai_context.synonyms`` becomes glossary entries, so agents find the member by synonym.
- Cosmos's own ``custom_extensions`` restore the model without loss; other vendors'
  extensions are kept in ``custom_extensions``.

Export (first slice: models, entities, dimensions, measures and metrics):

- Models become ``datasets``, dimensions and keys become ``fields``, entities become
  ``primary_key``, ``unique_keys`` and explicit ``relationships``.
- Measures and metrics that aggregate one dataset become ``metrics`` on that dataset, in
  ``OSSIE_SQL_2026``, written as the planner computes them: a filter becomes ``CASE WHEN``
  inside the aggregation, AVG runs in double precision and division is real and null on a zero
  denominator.
- The exported part of the project goes, whole, into a ``COSMOS_SEMANTIC`` extension, which
  ``import_ossie`` restores without loss. Metrics spanning datasets (a naive reader could fan
  them out), cumulative ones and filters Ossie cannot express live only there, with a warning.
- Glossary, business rules, row filters, sensitivity and business context are not exported
  yet; each one present becomes a warning, and the extension records what was omitted (names
  and counts only), which ``import_ossie`` reports again. Row filters and sensitivity make the
  export fail unless the caller accepts losing them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from cosmos_semantic.compiler import SemanticGraph, compile_project
from cosmos_semantic.errors import PolicyLossError, QueryError
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

    for ext in doc.get("custom_extensions") or []:
        if ext.get("vendor_name") == VENDOR:
            data = json.loads(ext["data"])
            if "project" in data:
                warnings += _omitted_warnings(data.get("omitted") or {})
                return ImportResult(Project.model_validate(data["project"]), warnings)

    datasets = {d["name"]: d for d in doc.get("datasets") or []}
    model_names = {name: _ident(name) for name in datasets}
    field_exprs: dict[str, dict[str, str]] = {}
    models: dict[str, dict[str, Any]] = {}
    glossary: list[dict[str, Any]] = []

    for name, ds in datasets.items():
        mname = model_names[name]
        where = f"datasets.{name}"
        model: dict[str, Any] = {"name": mname, "table": ds["source"]}
        if ds.get("description"):
            model["description"] = ds["description"]
        exprs: dict[str, str] = {}
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
            "custom_extensions": {"ossie": extensions} if extensions else {},
        }
    )
    return ImportResult(project, warnings)


def _omitted_warnings(omitted: dict[str, Any]) -> list[str]:
    """What the exporter left out of the document, as recorded in its marker."""
    out = []
    if omitted.get("row_filters"):
        out.append(
            "row_filters: the export omitted "
            + ", ".join(omitted["row_filters"])
            + "; this model has no row-level security"
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
# Members of the authoring format that a later slice maps to Ossie.
_LATER = ("glossary", "business_rules", "user_context", "teams", "systems", "processes")
_SPANS = (
    "it spans datasets; Ossie does not require a reader to aggregate each dataset at its own "
    "grain, so a native expression could fan out"
)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def export_ossie(
    project: Project,
    *,
    name: str = "semantic_model",
    fmt: str = "yaml",
    allow_policy_loss: bool = False,
) -> ExportResult:
    """Writes the project as an Ossie document (YAML or JSON), with a warning for each member
    this slice does not export. Refuses, unless ``allow_policy_loss``, a project with row
    filters or sensitive dimensions, which the document would lose."""
    if fmt not in ("yaml", "json"):
        raise ValueError(f"unknown format: {fmt} (use yaml or json)")
    compile_project(project)  # only valid models are exported
    warnings: list[str] = []
    exported, omitted = _exported_part(project, warnings)
    if (omitted.get("row_filters") or omitted.get("sensitivity")) and not allow_policy_loss:
        raise PolicyLossError(
            f"{_plural(len(omitted.get('row_filters', [])), 'row filter')} and "
            f"{_plural(len(omitted.get('sensitivity', [])), 'sensitive dimension')} would NOT be "
            "exported; the document would have no row-level security or personal-data marking. "
            "Pass --allow-policy-loss to export anyway"
        )
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
    """The project without the members a later slice exports, with a warning for each, and
    the marker of what was omitted: names and counts only, never values."""
    data = project.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    omitted: dict[str, Any] = {}
    row_filters: list[str] = []
    sensitive: list[str] = []
    for model in data.get("semantic_models", []):
        where = f"semantic_models.{model['name']}"
        filters = model.pop("row_filters", None)
        if filters:
            row_filters += [f"{model['name']}.{f['name']}" for f in filters]
            names = ", ".join(f["name"] for f in filters)
            warnings.append(
                f"{where}.row_filters: {names} not exported to Ossie yet; "
                "the exported model has no row-level security"
            )
        for dim in model.get("dimensions", []):
            if dim.pop("sensitivity", None):
                sensitive.append(f"{model['name']}.{dim['name']}")
                warnings.append(
                    f"{where}.dimensions.{dim['name']}: the sensitivity is not exported to "
                    "Ossie yet; the exported model treats it as ordinary data"
                )
    if row_filters:
        omitted["row_filters"] = row_filters
    if sensitive:
        omitted["sensitivity"] = sensitive
    for key in _LATER:
        items = data.pop(key, None)
        if items:
            omitted[key] = len(items)
            warnings.append(f"{key}: not exported to Ossie yet ({len(items)} in the project)")
    return Project.model_validate(data), omitted


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
            out.append(item)
        return out

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
                raise _NotNative(f"its filter uses the business rule `{text}`, not exported yet")
            return dim.name

        return sqlglot.parse_one(REF_RE.sub(ref, flt))
