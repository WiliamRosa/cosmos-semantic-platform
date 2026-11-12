"""Importing and exporting Cube models, static YAML only (ADR 0007).

Import: JavaScript models and YAML with Jinja or Python templates produce a warning and are not
imported, because they would require running user code. Access rules in ``cube.js`` are code
too: the report lists what must be recreated as ``row_filters`` and ``sensitivity``.

Export (first slice): one cube per model with ``sql_table``, joins from the entities, dimensions
(the primary key as a hidden dimension when no dimension holds it), measures, and metrics as
measures: a simple metric is a measure with ``filters``, ratio and derived metrics are ``number``
measures, with division real and null on a zero denominator and AVG in decimal, as the planner
computes them. ``import_cube`` reads the export back with the same numbers. Metrics spanning
cubes are exported with a warning (docs Rule 3). The export fails closed on row filters and
sensitivity, and on metrics it cannot carry (cumulative ones, filters beyond the measure's own
cube), unless the caller accepts the loss. Business rules, the glossary and the business context
are not exported yet; each one present is a warning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.tokens import TokenType

from cosmos_semantic.errors import QueryError
from cosmos_semantic.expressions import REF_RE, USER_RE
from cosmos_semantic.interchange import ExportResult, ImportResult
from cosmos_semantic.spec import DerivedMetric, Project, RatioMetric, SimpleMetric

_CUBE_REF = re.compile(r"\$?\{CUBE\}\.")
_MEMBER_REF = re.compile(r"\$?\{([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?\}")
_TEMPLATE = re.compile(r"\{%|\{\{")
_DIM_TYPE = {"string": "categorical", "number": "number", "time": "time", "boolean": "boolean"}
_AGG = {
    "count": "count",
    "count_distinct": "count_distinct",
    "count_distinct_approx": "count_distinct",
    "sum": "sum",
    "avg": "avg",
    "min": "min",
    "max": "max",
}
_MANY_TO_ONE = {"many_to_one", "belongs_to", "belongsTo", "one_to_one", "has_one", "hasOne"}
_ONE_TO_MANY = {"one_to_many", "has_many", "hasMany"}


def _strip_cube(sql: str) -> str:
    return _CUBE_REF.sub("", str(sql))


def import_cube(path: str | Path, *, schema: str | None = None) -> ImportResult:
    root = Path(path)
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    warnings: list[str] = []
    cubes: list[dict] = []
    extensions: dict[str, Any] = {}
    for file in files:
        if file.suffix in (".js", ".ts"):
            warnings.append(
                f"{file.name}: JavaScript models are not imported (they would require running code)"
            )
            continue
        if file.suffix == ".py":
            warnings.append(f"{file.name}: Cube Python files are not imported")
            continue
        if file.suffix not in (".yml", ".yaml"):
            continue
        text = file.read_text(encoding="utf-8")
        if _TEMPLATE.search(text):
            warnings.append(f"{file.name}: YAML with Jinja/Python templates is not imported")
            continue
        data = yaml.safe_load(text) or {}
        cubes.extend(data.get("cubes") or [])
        if data.get("views"):
            extensions.setdefault("views", []).extend(data["views"])
            warnings.append(
                f"{file.name}: `views` have no MVP equivalent; kept in custom_extensions"
            )
    if any(f.name in ("cube.js", "cube.py") for f in files):
        warnings.append(
            "cube.js/cube.py found: access rules (queryRewrite, contextToAppId) are code; "
            "recreate them as row_filters and sensitivity"
        )

    models: dict[str, dict[str, Any]] = {}
    pk: dict[str, str] = {}
    col_to_dim: dict[str, dict[str, str]] = {}
    metrics: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []

    for cube in cubes:
        name = cube["name"]
        where = f"cubes.{name}"
        table = cube.get("sql_table")
        if not table and cube.get("sql"):
            table = _table_from_sql(cube["sql"])
            if not table:
                warnings.append(f"{where}: `sql` is not a SELECT * from a table; cube skipped")
                continue
        if not table:
            warnings.append(f"{where}: no `sql_table`; cube skipped")
            continue
        if schema and "." not in table:
            table = f"{schema}.{table}"
        model: dict[str, Any] = {"name": name, "table": table}
        if cube.get("description"):
            model["description"] = cube["description"]
        col_to_dim[name] = {}
        for dim in cube.get("dimensions") or []:
            dname, sql = dim["name"], _strip_cube(dim.get("sql", dim["name"]))
            if _MEMBER_REF.search(sql):
                warnings.append(f"{where}.{dname}: dimension referencing another member; skipped")
                continue
            if dim.get("primary_key"):
                model.setdefault("entities", []).append(
                    {"name": name, "type": "primary", **({"expr": sql} if sql != name else {})}
                )
                pk[name] = sql
                if not dim.get("public", dim.get("shown", True)):
                    continue
            kind = _DIM_TYPE.get(dim.get("type", "string"))
            if kind is None:
                warnings.append(
                    f"{where}.{dname}: type `{dim.get('type')}` is not supported; skipped"
                )
                continue
            item = {"name": dname, "type": kind}
            if sql != dname:
                item["expr"] = sql
            if dim.get("description"):
                item["description"] = dim["description"]
            model.setdefault("dimensions", []).append(item)
            col_to_dim[name][sql] = dname
        if cube.get("access_policy"):
            warnings.append(
                f"{where}: `access_policy` must be recreated as row_filters and sensitivity"
            )
        for key in ("pre_aggregations", "refresh_key", "segments_meta", "hierarchies", "folders"):
            if cube.get(key):
                extensions.setdefault("cubes", {}).setdefault(name, {})[key] = cube[key]
                if key == "pre_aggregations":
                    warnings.append(
                        f"{where}: pre_aggregations kept in custom_extensions (no cache in the MVP)"
                    )
        models[name] = model

    for cube in cubes:
        name = cube["name"]
        if name not in models:
            continue
        model = models[name]
        for measure in cube.get("measures") or []:
            _measure(name, model, measure, col_to_dim[name], metrics, warnings)
        for segment in cube.get("segments") or []:
            rule = _segment(name, segment, col_to_dim[name], models[name], warnings)
            if rule:
                rules.append(rule)
        for join in cube.get("joins") or []:
            _join(name, join, models, pk, warnings)

    project = Project.model_validate(
        {
            "version": 1,
            "semantic_models": list(models.values()),
            "metrics": metrics,
            "business_rules": rules,
            "custom_extensions": {"cube": extensions} if extensions else {},
        }
    )
    return ImportResult(project, warnings)


def _table_from_sql(sql: str) -> str | None:
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return None
    if (
        isinstance(tree, exp.Select)
        and len(tree.expressions) == 1
        and isinstance(tree.expressions[0], exp.Star)
        and not tree.args.get("where")
        and not tree.args.get("joins")
        and isinstance(tree.args.get("from_") or tree.args.get("from"), exp.From)
    ):
        source = (tree.args.get("from_") or tree.args.get("from")).this
        if isinstance(source, exp.Table):
            return ".".join(p.name for p in source.parts)
    return None


def _cube_filter(sql: str, dims: dict[str, str]) -> str:
    """``{CUBE}.status = 'x'`` becomes ``{status} = 'x'`` when the column is a dimension."""
    text = _strip_cube(sql)
    tree = sqlglot.parse_one(text)

    def swap(node):
        if isinstance(node, exp.Column) and not node.table and node.name in dims:
            return exp.column(f"__dim_{dims[node.name]}")
        return node

    out = tree.transform(swap).sql()
    return re.sub(r"__dim_([a-z0-9_]+)", lambda m: "{" + m.group(1) + "}", out)


def _measure(cube, model, measure, dims, metrics, warnings) -> None:
    name, kind = measure["name"], measure.get("type", "count")
    where = f"cubes.{cube}.measures.{name}"
    sql = measure.get("sql")
    if kind == "number":
        refs = _MEMBER_REF.findall(str(sql or ""))
        if not refs:
            warnings.append(f"{where}: `number` measure without measure references; skipped")
            return
        expr = _MEMBER_REF.sub(lambda m: "{" + (m.group(2) or m.group(1)) + "}", str(sql))
        metrics.append({"name": name, "type": "derived", "expr": _strip_cube(expr)})
        return
    if kind not in _AGG:
        warnings.append(f"{where}: type `{kind}` is not supported; skipped")
        return
    if kind == "count_distinct_approx":
        warnings.append(f"{where}: count_distinct_approx imported as an exact count")
    filters = [f["sql"] for f in measure.get("filters") or [] if f.get("sql")]
    window = measure.get("rolling_window")
    base_name = f"{name}__base" if filters or window else name
    item: dict[str, Any] = {"name": base_name, "agg": _AGG[kind]}
    if sql is not None and str(sql).strip() not in ("*", "1"):
        item["expr"] = _strip_cube(sql)
    if measure.get("description") and base_name == name:
        item["description"] = measure["description"]
    model.setdefault("measures", []).append(item)
    if window:
        metric: dict[str, Any] = {"name": name, "type": "cumulative", "measure": base_name}
        if window.get("type") == "to_date" and window.get("granularity"):
            metric["grain_to_date"] = window["granularity"]
        else:
            metric["window"] = str(window.get("trailing", "unbounded"))
            warnings.append(
                f"{where}: rolling windows are not in the MVP; queries using the metric will be refused"
            )
        if filters:
            warnings.append(f"{where}: filters on a cumulative measure were dropped")
        metrics.append(metric)
    elif filters:
        flt = " AND ".join(f"({_cube_filter(f, dims)})" for f in filters)
        metrics.append({"name": name, "type": "simple", "measure": base_name, "filter": flt})


def _segment(cube, segment, dims, model, warnings) -> dict | None:
    name = segment["name"]
    sql = _strip_cube(segment.get("sql", ""))
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        warnings.append(f"cubes.{cube}.segments.{name}: invalid SQL; skipped")
        return None
    columns = sorted({c.name for c in tree.find_all(exp.Column)})
    types = {d["name"]: d["type"] for d in model.get("dimensions", [])}
    inputs, bind = {}, {}
    for col in columns:
        dim = dims.get(col)
        inputs[col] = types.get(dim, "categorical") if dim else "categorical"
        bind[col] = dim or col
    logic = tree.transform(
        lambda n: exp.column(f"__in_{n.name}") if isinstance(n, exp.Column) and not n.table else n
    ).sql()
    logic = re.sub(r"__in_([A-Za-z0-9_]+)", lambda m: "{" + m.group(1) + "}", logic)
    warnings.append(
        f"cubes.{cube}.segments.{name}: became rule `{name}`; set `owner` and `effective_from`"
    )
    return {
        "name": name,
        "kind": "predicate",
        "description": segment.get("description") or f"Segment `{name}` imported from Cube",
        "inputs": inputs,
        "logic": logic,
        "applies_to": [{"model": cube, "bind": bind}],
    }


def _join(cube, join, models, pk, warnings) -> None:
    target = join["name"]
    where = f"cubes.{cube}.joins.{target}"
    if target not in models:
        warnings.append(f"{where}: target cube was not imported; join skipped")
        return
    sides = _equality(join.get("sql", ""), cube, target)
    if sides is None:
        warnings.append(f"{where}: only simple equality joins between two columns are imported")
        return
    local_col, remote_col = sides
    relationship = join.get("relationship", "many_to_one")
    if relationship in _MANY_TO_ONE:
        _link(models, pk, source=cube, source_col=local_col, target=target, target_col=remote_col)
    elif relationship in _ONE_TO_MANY:
        _link(models, pk, source=target, source_col=remote_col, target=cube, target_col=local_col)
    else:
        warnings.append(f"{where}: unknown relationship `{relationship}`; join skipped")


def _link(models, pk, *, source, source_col, target, target_col) -> None:
    target_model = models[target]
    if pk.get(target) == target_col:
        entity = target
    else:
        entity = f"{target}_{target_col}"
        if not any(e["name"] == entity for e in target_model.get("entities", [])):
            target_model.setdefault("entities", []).append(
                {"name": entity, "type": "unique", "expr": target_col}
            )
    source_model = models[source]
    if not any(e["name"] == entity for e in source_model.get("entities", [])):
        source_model.setdefault("entities", []).append(
            {"name": entity, "type": "foreign", "expr": source_col}
        )


def _equality(sql: str, cube: str, target: str) -> tuple[str, str] | None:
    """Reads ``{CUBE}.a = {target}.b`` (in either order) and returns (a, b)."""
    text = re.sub(r"\$?\{CUBE\}", f"__c_{cube}", sql)
    text = re.sub(
        r"\$?\{([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: f"__c_{m.group(1)}.{m.group(2)}",
        text,
    )
    text = re.sub(r"\$?\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: f"__c_{m.group(1)}", text)
    try:
        tree = sqlglot.parse_one(text)
    except sqlglot.errors.ParseError:
        return None
    if not isinstance(tree, exp.EQ):
        return None
    left, right = tree.this, tree.expression
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return None
    owners = {
        left.table.removeprefix("__c_"): left.name,
        right.table.removeprefix("__c_"): right.name,
    }
    if cube not in owners or target not in owners:
        return None
    return owners[cube], owners[target]


# -- export -----------------------------------------------------------------------------------

_CUBE_TYPE = {"categorical": "string", "number": "number", "time": "time", "boolean": "boolean"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Members of the authoring format that a later slice maps to Cube.
_CUBE_LATER = ("business_rules", "glossary", "teams", "systems", "processes")
_SPANS_CUBES = (
    "Cube must aggregate each cube at its own grain to return the same numbers (Rule 3 of "
    "docs/interoperability.md); check them in Cube"
)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _prefixed(text: str, prefix: str) -> str:
    """Model SQL with ``prefix.`` before each bare column; the rest of the text is kept as
    written, so ``import_cube`` gets the same expression back by removing ``{CUBE}.``."""
    if _IDENT.fullmatch(text):
        return f"{prefix}.{text}"
    try:
        tree = sqlglot.parse_one(text)
        tokens = sqlglot.Tokenizer().tokenize(text)
    except sqlglot.errors.SqlglotError:
        return text
    names = {c.name for c in tree.find_all(exp.Column) if not c.table}
    out, last = [], 0
    for i, token in enumerate(tokens):
        if token.text not in names or token.token_type == TokenType.STRING:
            continue
        if i and tokens[i - 1].token_type == TokenType.DOT:
            continue
        if i + 1 < len(tokens) and tokens[i + 1].token_type in (TokenType.DOT, TokenType.L_PAREN):
            continue
        out += [text[last : token.start], f"{prefix}."]
        last = token.start
    return "".join(out) + text[last:]


def _cube_sql(text: str) -> str:
    return _prefixed(text, "{CUBE}")


class _NotExported(Exception):
    """A metric the Cube export cannot carry; the message is the reason."""


def export_cube(
    project: Project,
    *,
    allow_policy_loss: bool = False,
    allow_metric_loss: bool = False,
) -> ExportResult:
    """Writes the project as Cube static YAML, with a warning for each member this slice does
    not export. Refuses, unless ``allow_policy_loss``, a project with row filters or
    sensitivity, and, unless ``allow_metric_loss``, one with metrics Cube cannot carry here."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.errors import MetricLossError, PolicyLossError

    graph = compile_project(project)  # only valid models are exported
    warnings: list[str] = []

    row_filters = [f"{m.name}.{f.name}" for m in project.semantic_models for f in m.row_filters]
    sensitive = [
        f"{m.name}.{d.name}" for m in project.semantic_models for d in m.dimensions if d.sensitivity
    ]
    if (row_filters or sensitive) and not allow_policy_loss:
        parts = []
        if row_filters:
            parts.append(f"{_plural(len(row_filters), 'row filter')} ({', '.join(row_filters)})")
        if sensitive:
            parts.append(
                f"the sensitivity of {_plural(len(sensitive), 'dimension')} "
                f"({', '.join(sensitive)})"
            )
        raise PolicyLossError(
            f"{' and '.join(parts)} would NOT be exported: the Cube export does not carry access "
            "or personal-data policies yet; the Cube model would not have this row-level "
            "security or personal-data marking. Pass --allow-policy-loss to export anyway"
        )
    for name in row_filters:
        model, member = name.split(".", 1)
        warnings.append(
            f"semantic_models.{model}.row_filters.{member}: not exported to Cube; the Cube model "
            "does not have this row-level security"
        )
    for name in sensitive:
        model, member = name.split(".", 1)
        warnings.append(
            f"semantic_models.{model}.dimensions.{member}: its sensitivity is not exported to "
            "Cube; the Cube model does not mark it as personal data"
        )

    writer = _CubeWriter(graph, warnings)
    if writer.lost and not allow_metric_loss:
        reasons = "; ".join(f"{k}: {v}" for k, v in writer.lost.items())
        raise MetricLossError(
            f"{_plural(len(writer.lost), 'metric')} ({', '.join(writer.lost)}) would NOT be "
            f"exported ({reasons}); the Cube model would not have them. Pass "
            "--allow-metric-loss to export anyway"
        )
    for name, reason in writer.lost.items():
        warnings.append(f"metrics.{name}: {reason}; it is not exported to Cube")

    if project.user_context:
        warnings.append(
            f"user_context: {', '.join(project.user_context)} not exported to Cube (Cube reads "
            "the user from its security context)"
        )
    if project.timezone:
        warnings.append(
            f"timezone: Cube models have no time zone; set `timezone: {project.timezone}` on "
            "Cube queries so that dates mean the same day"
        )
    for key in _CUBE_LATER:
        items = getattr(project, key)
        if items:
            warnings.append(f"{key}: not exported to Cube yet ({len(items)} in the project)")
    if project.custom_extensions:
        warnings.append(
            f"custom_extensions: {', '.join(project.custom_extensions)} not exported to Cube yet"
        )
    text = yaml.safe_dump({"cubes": writer.cubes()}, sort_keys=False, allow_unicode=True, width=100)
    return ExportResult(text, list(dict.fromkeys(warnings)))


class _CubeWriter:
    """Builds the cubes from a compiled graph and decides where each metric goes."""

    def __init__(self, graph, warnings: list[str]) -> None:
        self.graph = graph
        self.warnings = warnings
        self.lost: dict[str, str] = {}
        self.placed: dict[str, str] = {}  # member -> cube it is written on
        self.leaves: dict[str, set[str]] = {}  # member -> cubes whose measures it aggregates
        self.measures: dict[str, list[dict[str, Any]]] = {m: [] for m in graph.models}
        for node in graph.models.values():
            for measure in node.spec.measures:
                self.placed[measure.name] = node.name
                self.leaves[measure.name] = {node.name}
                self.measures[node.name].append(self._measure(measure))
        for metric in graph.project.metrics:
            self._place(metric.name)

    # -- cubes ------------------------------------------------------------------------------

    def cubes(self) -> list[dict[str, Any]]:
        out = []
        for node in self.graph.models.values():
            spec = node.spec
            cube: dict[str, Any] = {"name": spec.name, "sql_table": spec.table}
            if spec.data_source:
                cube["data_source"] = spec.data_source
            if spec.description:
                cube["description"] = spec.description
            if spec.owner:
                cube["meta"] = {"owner": spec.owner}
            joins = self._joins(spec.name)
            if joins:
                cube["joins"] = joins
            cube["dimensions"] = self._dimensions(spec)
            if self.measures[spec.name]:
                cube["measures"] = self.measures[spec.name]
            out.append(cube)
        return out

    def _dimensions(self, spec) -> list[dict[str, Any]]:
        dims: list[dict[str, Any]] = []
        for dim in spec.dimensions:
            item: dict[str, Any] = {
                "name": dim.name,
                "sql": _cube_sql(dim.expr or dim.name),
                "type": _CUBE_TYPE[dim.type],
            }
            if dim.description:
                item["description"] = dim.description
            dims.append(item)
        primary = next((e for e in spec.entities if e.type == "primary"), None)
        if primary is None:
            return dims
        # Cube's primary key is a dimension: the one with the key's expression, or a hidden one.
        key = primary.expr or primary.name
        same = next((d for d in dims if d["sql"] == _cube_sql(key)), None)
        if same is not None:
            same["primary_key"] = True
            return dims
        taken = {d.name for d in spec.dimensions} | {m["name"] for m in self.measures[spec.name]}
        base = key if _IDENT.fullmatch(key) else f"{primary.name}_key"
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        hidden = {"name": name, "sql": _cube_sql(key), "type": "string"}
        return [{**hidden, "primary_key": True, "public": False}, *dims]

    def _joins(self, model: str) -> list[dict[str, Any]]:
        """The many-to-one joins the engine infers from entities, on the many side."""
        out = []
        for edge in self.graph.edges.get(model, []):
            local, remote = edge.source_expr, edge.target_expr
            if not (_IDENT.fullmatch(local) and _IDENT.fullmatch(remote)):
                self.warnings.append(
                    f"semantic_models.{model}.entities.{edge.entity}: the join to "
                    f"{edge.target} uses an expression; Cube gets it, but `cosmos import cube` "
                    "reads back only joins between two columns"
                )
            target = _prefixed(remote, "{" + edge.target + "}")
            out.append(
                {
                    "name": edge.target,
                    "relationship": "many_to_one",
                    "sql": f"{_cube_sql(local)} = {target}",
                }
            )
        return out

    # -- measures and metrics ---------------------------------------------------------------

    def _measure(self, measure, *, name: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {"name": name or measure.name, "type": measure.agg}
        text = measure.expr or (None if measure.agg == "count" else measure.name)
        if text is not None:
            sql = _cube_sql(text)
            if measure.agg == "avg":
                # In decimal, as the planner averages: an integer AVG truncates in SQL Server.
                sql = f"1.0 * {sql}" if _IDENT.fullmatch(text) else f"1.0 * ({sql})"
            item["sql"] = sql
        if name is None and measure.description:
            item["description"] = measure.description
        return item

    def _ref(self, member: str, cube: str) -> str:
        where = self.placed[member]
        return "{" + (member if where == cube else f"{where}.{member}") + "}"

    def _use(self, ref: str) -> str:
        """A member a metric references, placed first when it is a metric."""
        name = ref.split(".")[-1]
        if name not in self.placed and name in self.graph.metrics:
            self._place(name)
        if name not in self.placed:
            raise _NotExported(f"it uses `{name}`, which is not exported")
        return name

    def _place(self, name: str) -> None:
        if name in self.placed or name in self.lost:
            return
        metric = self.graph.metrics[name]
        try:
            cube, item, leaves = self._metric(metric)
        except _NotExported as exc:
            self.lost[name] = str(exc)
            return
        if metric.label:
            item["title"] = metric.label
        if metric.description:
            item["description"] = metric.description
        if metric.owner:
            item["meta"] = {"owner": metric.owner}
        self.placed[name] = cube
        self.leaves[name] = leaves
        self.measures[cube].append(item)
        if len(leaves) > 1:
            self.warnings.append(
                f"metrics.{name}: it spans cubes ({', '.join(sorted(leaves))}); {_SPANS_CUBES}"
            )

    def _metric(self, metric) -> tuple[str, dict[str, Any], set[str]]:
        if isinstance(metric, SimpleMetric):
            node, measure = self.graph.measure(metric.measure)
            item = self._measure(measure, name=metric.name)
            if metric.filter:
                item["filters"] = [{"sql": self._condition(metric.filter, node.name)}]
            return node.name, item, {node.name}
        if isinstance(metric, RatioMetric):
            if metric.filter:
                raise _NotExported(
                    "it has a filter, and a Cube `number` measure cannot filter the measures "
                    "it uses"
                )
            num, den = self._use(metric.numerator), self._use(metric.denominator)
            cube = self.placed[num]
            sql = f"1.0 * {self._ref(num, cube)} / NULLIF({self._ref(den, cube)}, 0)"
            item = {"name": metric.name, "type": "number", "sql": sql}
            return cube, item, self.leaves[num] | self.leaves[den]
        if isinstance(metric, DerivedMetric):
            return self._derived(metric)
        raise _NotExported(
            "it is cumulative, and the Cube export does not carry cumulative metrics"
        )

    def _derived(self, metric) -> tuple[str, dict[str, Any], set[str]]:
        refs: list[str] = []

        def mask(match):
            refs.append(match.group(1))
            return f"__ref_{len(refs) - 1}"

        tree = sqlglot.parse_one(REF_RE.sub(mask, metric.expr))
        bare = [c.sql() for c in tree.find_all(exp.Column) if not c.name.startswith("__ref_")]
        if bare:
            raise _NotExported(
                f"its expression uses bare columns ({', '.join(bare)}), and a Cube `number` "
                "measure references measures only"
            )
        names = [self._use(ref) for ref in refs]
        cube = self.placed[names[0]]
        leaves: set[str] = set().union(*(self.leaves[n] for n in names))
        if not tree.find(exp.Div):
            it = iter(names)
            sql = REF_RE.sub(lambda m: self._ref(next(it), cube), metric.expr)
        else:
            # Division as the planner computes it: real, and null on a zero denominator.
            # Innermost first, so a division inside another one is rewritten too.
            for node in reversed(list(tree.find_all(exp.Div, bfs=False))):
                real = exp.Div(
                    this=exp.Mul(this=exp.Literal.number("1.0"), expression=node.this.copy()),
                    expression=exp.Nullif(
                        this=node.expression.copy(), expression=exp.Literal.number(0)
                    ),
                )
                if node is tree:
                    tree = exp.Paren(this=real)
                else:
                    node.replace(exp.Paren(this=real))
            sql = re.sub(
                r"__ref_(\d+)",
                lambda m: self._ref(names[int(m.group(1))], cube),
                tree.sql(),
            )
        return cube, {"name": metric.name, "type": "number", "sql": sql}, leaves

    def _condition(self, flt: str, model: str) -> str:
        """A metric filter over the measure's own cube, as Cube SQL."""
        if USER_RE.search(flt):
            raise _NotExported("its filter uses $user, which only Cosmos resolves")
        node = self.graph.models[model]
        own = {d.name for d in node.spec.dimensions}

        def ref(match):
            text = match.group(1)
            try:
                dim = self.graph.resolve_dimension(text, context=model)
            except QueryError as exc:
                raise _NotExported(
                    f"its filter reference `{text}` does not resolve ({exc})"
                ) from None
            if dim.model != model:
                raise _NotExported(f"its filter uses `{text}` from another cube ({dim.model})")
            if dim.grain:
                raise _NotExported(f"its filter uses a time grain (`{text}`)")
            if dim.name not in own:
                raise _NotExported(f"its filter uses the business rule `{text}`, not exported yet")
            spec = node.dimensions[dim.name]
            text = spec.expr or spec.name
            return _cube_sql(text) if _IDENT.fullmatch(text) else f"({_cube_sql(text)})"

        return REF_RE.sub(ref, flt)
