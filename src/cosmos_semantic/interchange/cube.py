"""Importing Cube models, static YAML only (ADR 0007).

JavaScript models and YAML with Jinja or Python templates produce a warning and are not
imported, because they would require running user code. Access rules in ``cube.js`` are code
too: the report lists what must be recreated as ``row_filters`` and ``sensitivity``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from cosmos_semantic.interchange import ImportResult
from cosmos_semantic.spec import Project

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
