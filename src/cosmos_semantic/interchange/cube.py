"""Importing and exporting Cube models, static YAML only (ADR 0007).

Import: JavaScript models and YAML with Jinja or Python templates produce a warning and are not
imported, because they would require running user code. Access rules in ``cube.js`` are code
too: the report lists what must be recreated as ``row_filters`` and ``sensitivity``.

Export (first and second slices): one cube per model with ``sql_table``, joins from the
entities, dimensions (the primary key as a hidden dimension when no dimension holds it), measures,
and metrics as measures: a simple metric is a measure with ``filters``, ratio and derived metrics
are ``number`` measures, and a ``grain_to_date`` running total of a ``sum`` or ``count`` measure
is a measure with ``rolling_window: {type: to_date}``. Division and AVG are written with a
``1e0 *`` factor, a floating-point literal in most databases (a high-precision numeric in
Postgres and Oracle), so they are real and division is null on a zero denominator, close to the
planner's ``CAST(x AS DOUBLE)`` without being the same SQL. ``import_cube`` reads the export back
with the same numbers in Cosmos. Metrics spanning cubes are exported with a warning (docs Rule 3).
Third slice (DEC-20): row filters become one ``access_policy`` per cube for every role
(``role: "*"``) with ``row_level`` filters; ``$user.<field>`` becomes
``{ securityContext.<claim> }``; personal and sensitive dimensions become ``member_level``
excludes, with the level in the dimension's ``meta``. Only filters with an exact Cube equivalent
are written (``=``, ``IN``, and ``<``, ``<=``, ``>``, ``>=`` on numbers, joined by AND and OR);
``import_cube`` reads them back as the same row filters and sensitivity, and fails closed on any
other policy (it would grant more access than Cube did) unless the caller accepts the loss.
Fourth slice (DEC-30): a ``window: N days`` over a ``sum``, ``count``, ``min`` or ``max``
measure is a measure with ``rolling_window: {trailing: N day, offset: end}``, which
``import_cube`` reads back as the same window.
The export fails closed on row filters it cannot express, and on metrics it cannot carry
(running totals of other aggregations or without a period, metrics over a cumulative one,
offsets, filters beyond the measure's own cube), unless the caller accepts the loss.
Business rules, the glossary and the business context are not exported yet; each one present is
a warning.
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
from cosmos_semantic.spec import (
    WINDOW_AGGS,
    CumulativeMetric,
    DerivedMetric,
    Project,
    RatioMetric,
    SimpleMetric,
)

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


def import_cube(
    path: str | Path, *, schema: str | None = None, allow_policy_loss: bool = False
) -> ImportResult:
    """Reads Cube static YAML. Refuses, unless ``allow_policy_loss``, a cube whose
    ``access_policy`` Cosmos cannot express, since the imported model would grant more access
    than Cube did; with ``allow_policy_loss``, each cube's loss is a warning."""
    from cosmos_semantic.errors import PolicyLossError

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
    user_context: dict[str, dict[str, Any]] = {}
    lost: dict[str, list[str]] = {}  # cube -> why its access_policy is not imported in full

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
            level = (dim.get("meta") or {}).get("sensitivity")
            if level in ("personal", "sensitive"):
                item["sensitivity"] = level
            model.setdefault("dimensions", []).append(item)
            col_to_dim[name][sql] = dname
        if cube.get("access_policy"):
            filters = _read_policy(where, cube, model, user_context, lost)
            if filters:
                model["row_filters"] = filters
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

    if lost and not allow_policy_loss:
        reasons = "; ".join(f"cubes.{c}: {', and '.join(r)}" for c, r in lost.items())
        raise PolicyLossError(
            f"the access_policy of {_plural(len(lost), 'cube')} ({', '.join(lost)}) would NOT "
            f"be imported in full ({reasons}); the Cosmos model would grant more access than "
            "Cube did. Pass --allow-policy-loss to import anyway"
        )
    for cube_name, reasons in lost.items():
        for reason in reasons:
            warnings.append(
                f"cubes.{cube_name}: access_policy not imported in full ({reason}); the Cosmos "
                "model grants more access than Cube did, so recreate it as row_filters and "
                "sensitivity"
            )

    project = Project.model_validate(
        {
            "version": 1,
            "semantic_models": list(models.values()),
            "metrics": metrics,
            "business_rules": rules,
            "user_context": user_context,
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


def _trailing_days(window: dict[str, Any]) -> int | None:
    """N for a Cube rolling window with ``trailing: N day`` (or ``N days``), None otherwise."""
    if window.get("type") not in (None, "fixed"):
        return None
    match = re.fullmatch(r"\s*(\d+)\s+days?\s*", str(window.get("trailing", "")), re.IGNORECASE)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


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
    window = measure.get("rolling_window")
    days = _trailing_days(window) if window else None
    if days and (window.get("offset", "end") != "end" or "leading" in window):
        warnings.append(
            f"{where}: a trailing window in days with `offset: start` or `leading` does not "
            "end on the row's day, as Cosmos windows do; measure skipped"
        )
        return
    if days and _AGG[kind] not in WINDOW_AGGS:
        warnings.append(
            f"{where}: a trailing window over a measure of type `{kind}` cannot be combined from daily "
            "values in Cosmos (use sum, count, min or max); measure skipped"
        )
        return
    if kind == "count_distinct_approx":
        warnings.append(f"{where}: count_distinct_approx imported as an exact count")
    filters = [f["sql"] for f in measure.get("filters") or [] if f.get("sql")]
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
        elif days:
            # Cube's trailing window with `offset: end` ends at the end of each bucket: at day
            # grain, the N days ending on the row's day, as the planner's `window: N days`.
            metric["window"] = "1 day" if days == 1 else f"{days} days"
        else:
            metric["window"] = str(window.get("trailing", "unbounded"))
            warnings.append(
                f"{where}: only rolling windows in days (`trailing: 7 day`) are imported; "
                "queries using the metric will be refused"
            )
        if filters:
            warnings.append(f"{where}: filters on a cumulative measure were dropped")
        if measure.get("description"):
            metric["description"] = measure["description"]
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


# -- access policies --------------------------------------------------------------------------

# Cube row_level operators the export writes, with their SQL. `notEquals` and `notIn` are not
# among them: Cube keeps the rows where the member is null, and SQL's `<>` does not.
_POLICY_OPS = {"equals": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_SQL_OPS = {exp.EQ: "equals", exp.GT: "gt", exp.GTE: "gte", exp.LT: "lt", exp.LTE: "lte"}
_FLIPPED = {"equals": "equals", "gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte"}
_CONTEXT = re.compile(r"^\{\s*securityContext\.([A-Za-z_][A-Za-z0-9_]*)\s*\}$")
_NUMBER = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_FIELD = re.compile(r"^[a-z_][a-z0-9_]*$")


class _ClaimClash(ValueError):
    """Two security context attributes that would become the same ``$user`` field."""


def _read_policy(where, cube, model, user_context, lost) -> list[dict[str, Any]]:
    """The row filters of an ``access_policy`` of the shape ``export_cube`` writes: one policy
    for every role, with ``row_level`` filters and ``member_level`` excludes. What Cosmos cannot
    express goes in ``lost`` (cube -> reasons): importing without it grants more access than
    Cube did, so ``import_cube`` refuses it unless the caller accepts the loss."""
    policies = cube["access_policy"]
    policy = policies[0] if isinstance(policies, list) and len(policies) == 1 else None
    policy = policy if isinstance(policy, dict) else {}
    row, member = policy.get("row_level") or {}, policy.get("member_level") or {}
    if (
        not isinstance(row, dict)
        or not isinstance(member, dict)
        or policy.get("role") != "*"
        or set(policy) - {"role", "row_level", "member_level"}
        or set(row) - {"filters"}
        or set(member) - {"includes", "excludes"}
        or member.get("includes", "*") != "*"
        or not isinstance(member.get("excludes") or [], list)
    ):
        lost.setdefault(cube["name"], []).append(
            'it is not one policy for every role (`role: "*"`) with row_level filters and '
            "member_level excludes"
        )
        return []
    dims = {d["name"]: d for d in model.get("dimensions", [])}
    for name in member.get("excludes") or []:
        if not dims.get(name, {}).get("sensitivity"):
            lost.setdefault(cube["name"], []).append(
                f"it hides `{name}` from every user, which Cosmos expresses only as "
                "`sensitivity` (the dimension's `meta.sensitivity`)"
            )
    found: dict[str, dict[str, Any]] = {}
    try:
        texts = [
            _policy_sql(f, dims, found, cube["name"], top=True) for f in row.get("filters") or []
        ]
    except _ClaimClash as exc:
        raise _ClaimClash(f"{where}: {exc}") from None
    except ValueError as exc:
        lost.setdefault(cube["name"], []).append(f"its row_level filters cannot be read: {exc}")
        return []
    for field, spec in found.items():
        known = user_context.get(field)
        if known and known["claim"] != spec["claim"]:
            raise _ClaimClash(
                f"{where}: securityContext.{spec['claim']} and securityContext."
                f"{known['claim']} (another cube) would both become `$user.{field}`"
            )
        user_context[field] = spec
    names = (cube.get("meta") or {}).get("row_filters")
    if not (isinstance(names, list) and len(names) == len(texts)):
        names = [f"access_policy_{i + 1}" for i in range(len(texts))]
    return [{"name": n, "filter": t} for n, t in zip(names, texts, strict=True)]


def _policy_member(member: Any, cube: str) -> str:
    """The dimension a filter names: plain, or after the cube's own name, ``CUBE``,
    ``{CUBE}`` or ``${CUBE}``."""
    match = re.fullmatch(
        r"(?:(\$?\{CUBE\}|CUBE|[A-Za-z_][A-Za-z0-9_]*)\.)?([A-Za-z_]\w*)", str(member)
    )
    if not match or match.group(1) not in (None, cube, "CUBE", "{CUBE}", "${CUBE}"):
        raise ValueError(f"`{member}`, not a member of this cube")
    return match.group(2)


def _policy_sql(item, dims, found, cube: str, *, top: bool = False) -> str:
    """A row_level filter or ``and``/``or`` group as a row filter expression."""
    if not isinstance(item, dict):
        raise ValueError(f"`{item}` is not a filter")
    for key in ("and", "or"):
        if key in item:
            if set(item) != {key} or not isinstance(item[key], list) or not item[key]:
                raise ValueError(f"an `{key}` group with other keys or without filters")
            text = f" {key.upper()} ".join(_policy_sql(f, dims, found, cube) for f in item[key])
            return text if top else f"({text})"
    if set(item) != {"member", "operator", "values"}:
        raise ValueError(f"a filter with keys {', '.join(sorted(item))}")
    name = _policy_member(item["member"], cube)
    dim = dims.get(name)
    if dim is None or dim["type"] not in ("categorical", "number"):
        raise ValueError(f"`{item['member']}` is not a string or number dimension of the cube")
    op = _POLICY_OPS.get(item["operator"])
    if op is None:
        raise ValueError(f"the operator `{item['operator']}`")
    if op != "=" and dim["type"] != "number":
        raise ValueError(f"the operator `{item['operator']}` on `{name}`")
    values = item["values"]
    if not isinstance(values, list) or not values or (op != "=" and len(values) != 1):
        raise ValueError(f"the values of `{name}`")
    sql = [_policy_value(v, dim, found) for v in values]
    if len(sql) > 1:
        return f"{{{name}}} IN ({', '.join(sql)})"
    return f"{{{name}}} {op} {sql[0]}"


def _policy_value(value, dim, found) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"the value `{value}`")
    text = str(value)
    context = _CONTEXT.match(text)
    if context:
        claim = context.group(1)
        field = claim.lower()
        if not _FIELD.match(field):
            raise ValueError(f"the security context attribute `{claim}`")
        if found.get(field, {"claim": claim})["claim"] != claim:
            raise _ClaimClash(
                f"securityContext.{claim} and securityContext.{found[field]['claim']} would "
                f"both become `$user.{field}`"
            )
        found[field] = {"claim": claim}
        return f"$user.{field}"
    if "{" in text or "}" in text:
        raise ValueError(f"the template `{text}`")
    if dim["type"] == "number":
        if not _NUMBER.match(text):
            raise ValueError(f"`{text}` is not a number")
        return text
    return "'" + text.replace("'", "''") + "'"


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


def refuse_policy_loss(project: Project, target: str, allow: bool, warnings: list[str]) -> None:
    """Refuses, unless ``allow``, an export to ``target`` that would drop row filters or
    sensitivity; with ``allow``, each one dropped is a warning."""
    from cosmos_semantic.errors import PolicyLossError

    row_filters = [f"{m.name}.{f.name}" for m in project.semantic_models for f in m.row_filters]
    sensitive = [
        f"{m.name}.{d.name}" for m in project.semantic_models for d in m.dimensions if d.sensitivity
    ]
    if (row_filters or sensitive) and not allow:
        parts = []
        if row_filters:
            parts.append(f"{_plural(len(row_filters), 'row filter')} ({', '.join(row_filters)})")
        if sensitive:
            parts.append(
                f"the sensitivity of {_plural(len(sensitive), 'dimension')} "
                f"({', '.join(sensitive)})"
            )
        raise PolicyLossError(
            f"{' and '.join(parts)} would NOT be exported: the {target} export does not carry "
            f"access or personal-data policies yet; the {target} model would not have this "
            "row-level security or personal-data marking. Pass --allow-policy-loss to export anyway"
        )
    for name in row_filters:
        model, member = name.split(".", 1)
        warnings.append(
            f"semantic_models.{model}.row_filters.{member}: not exported to {target}; the "
            f"{target} model does not have this row-level security"
        )
    for name in sensitive:
        model, member = name.split(".", 1)
        warnings.append(
            f"semantic_models.{model}.dimensions.{member}: its sensitivity is not exported to "
            f"{target}; the {target} model does not mark it as personal data"
        )


def real_division(tree: exp.Expression) -> exp.Expression:
    """Each division as the planner computes it: real, and null on a zero denominator.
    Innermost first, so a division inside another one is rewritten too."""
    for node in reversed(list(tree.find_all(exp.Div, bfs=False))):
        real = exp.Div(
            this=exp.Mul(this=exp.Literal.number("1e0"), expression=node.this.copy()),
            expression=exp.Nullif(this=node.expression.copy(), expression=exp.Literal.number(0)),
        )
        if node is tree:
            tree = exp.Paren(this=real)
        else:
            node.replace(exp.Paren(this=real))
    return tree


# Measures whose running total a Cube ``to_date`` window computes as the planner does: the sum
# of the rows from the start of the period equals the running sum of the per-bucket values.
_TO_DATE_AGGS = ("sum", "count")


class _NotExported(Exception):
    """A metric the Cube export cannot carry; the message is the reason."""


def export_cube(
    project: Project,
    *,
    allow_policy_loss: bool = False,
    allow_metric_loss: bool = False,
) -> ExportResult:
    """Writes the project as Cube static YAML, with a warning for each member this slice does
    not export. Refuses, unless ``allow_policy_loss``, a project with row filters Cube's
    ``access_policy`` cannot express, and, unless ``allow_metric_loss``, one with metrics Cube
    cannot carry here."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.errors import MetricLossError, PolicyLossError

    graph = compile_project(project)  # only valid models are exported
    warnings: list[str] = []

    writer = _CubeWriter(graph, warnings)
    if writer.policy_lost and not allow_policy_loss:
        reasons = "; ".join(f"{k}: {v}" for k, v in writer.policy_lost.items())
        raise PolicyLossError(
            f"{_plural(len(writer.policy_lost), 'row filter')} "
            f"({', '.join(writer.policy_lost)}) would NOT be exported ({reasons}); the Cube "
            "model would show those rows to every user. Pass --allow-policy-loss to export anyway"
        )
    for name, reason in writer.policy_lost.items():
        model, member = name.split(".", 1)
        warnings.append(
            f"semantic_models.{model}.row_filters.{member}: {reason}; it is not exported to "
            "Cube, and the Cube model does not have this row-level security"
        )
    if writer.lost and not allow_metric_loss:
        reasons = "; ".join(f"{k}: {v}" for k, v in writer.lost.items())
        raise MetricLossError(
            f"{_plural(len(writer.lost), 'metric')} ({', '.join(writer.lost)}) would NOT be "
            f"exported ({reasons}); the Cube model would not have them. Pass "
            "--allow-metric-loss to export anyway"
        )
    for name, reason in writer.lost.items():
        warnings.append(f"metrics.{name}: {reason}; it is not exported to Cube")

    if writer.claims:
        fields = ", ".join(f"$user.{f} as securityContext.{c}" for f, c in writer.claims.items())
        warnings.append(
            f"user_context: the row filters read {fields}. Cosmos denies a query when the user "
            "lacks one; Cube must reject such a security context too (in checkAuth), or its "
            "filters may not hide the rows"
        )
    if project.timezone:
        warnings.append(
            f"timezone: not exported to Cube. Model timestamps are local time, so keep Cube's "
            "query timezone at UTC (Cube would otherwise convert them and shift day, week and "
            "month buckets); relative dates such as today then follow UTC in Cube, not "
            f"{project.timezone}"
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
        self.policy_lost: dict[str, str] = {}  # model.row_filter -> why it is not exported
        self.claims: dict[str, str] = {}  # $user field -> security context attribute
        self.policies = {name: self._policy(node.spec) for name, node in graph.models.items()}

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
            policy, names = self.policies[spec.name]
            meta = {"owner": spec.owner} if spec.owner else {}
            if names:
                meta["row_filters"] = names  # so `cosmos import cube` gets the names back
            if meta:
                cube["meta"] = meta
            joins = self._joins(spec.name)
            if joins:
                cube["joins"] = joins
            cube["dimensions"] = self._dimensions(spec)
            if self.measures[spec.name]:
                cube["measures"] = self.measures[spec.name]
            if policy:
                cube["access_policy"] = [policy]
            out.append(cube)
        return out

    # -- access policy ----------------------------------------------------------------------

    def _policy(self, spec) -> tuple[dict[str, Any] | None, list[str]]:
        """One policy for every role: Cube grants each matching policy's rows, so two
        policies would OR the row filters, and a role without a policy would see nothing or,
        with ``role: "*"`` missing, bypass it. All row filters go in one ``row_level`` list,
        which Cube ANDs, as the planner does."""
        filters, names = [], []
        for row_filter in spec.row_filters:
            try:
                filters.append(self._row_filter(row_filter.filter, spec))
            except _NotExported as exc:
                self.policy_lost[f"{spec.name}.{row_filter.name}"] = str(exc)
                continue
            names.append(row_filter.name)
        hidden = [d.name for d in spec.dimensions if d.sensitivity]
        if not (filters or hidden):
            return None, []
        policy: dict[str, Any] = {"role": "*"}
        if filters:
            policy["row_level"] = {"filters": filters}
        if hidden:
            policy["member_level"] = {"includes": "*", "excludes": hidden}
        return policy, names

    def _row_filter(self, text: str, spec) -> dict[str, Any]:
        refs: list[str] = []
        users: list[str] = []

        def mask_ref(match):
            refs.append(match.group(1))
            return f"__ref_{len(refs) - 1}"

        def mask_user(match):
            users.append(match.group(1))
            return f"__user_{len(users) - 1}"

        for ref in REF_RE.findall(text):
            if ref.startswith("rule."):
                raise _NotExported(f"it reads the business rule `{ref}`")
        try:
            tree = sqlglot.parse_one(USER_RE.sub(mask_user, REF_RE.sub(mask_ref, text)))
        except sqlglot.errors.SqlglotError:
            raise _NotExported("its filter does not parse") from None

        def shown(node: exp.Expression) -> str:
            sql = re.sub(r"__ref_(\d+)", lambda m: "{" + refs[int(m.group(1))] + "}", node.sql())
            return re.sub(r"__user_(\d+)", lambda m: "$user." + users[int(m.group(1))], sql)

        def member(node: exp.Expression):
            if not (
                isinstance(node, exp.Column) and not node.table and node.name.startswith("__ref_")
            ):
                raise _NotExported(f"`{shown(node)}` is not a dimension of the cube")
            text = refs[int(node.name.removeprefix("__ref_"))]
            try:
                dim = self.graph.resolve_dimension(text, context=spec.name)
            except QueryError as exc:
                raise _NotExported(f"`{text}` does not resolve ({exc})") from None
            if dim.model != spec.name:
                raise _NotExported(f"it uses `{text}` from another cube ({dim.model})")
            if dim.grain:
                raise _NotExported(f"it uses a time grain (`{text}`)")
            own = next((d for d in spec.dimensions if d.name == dim.name), None)
            if own is None:
                raise _NotExported(f"it reads the business rule `{text}`")
            if own.type not in ("categorical", "number"):
                raise _NotExported(f"it filters the {own.type} dimension `{text}`")
            if own.sensitivity:
                raise _NotExported(
                    f"it filters `{text}`, which member_level hides, and a policy on a hidden "
                    "member is not checked against Cube's runtime"
                )
            return own

        def value(node: exp.Expression, dim) -> str:
            if isinstance(node, exp.Column) and node.name.startswith("__user_"):
                field = users[int(node.name.removeprefix("__user_"))]
                claim = self.graph.project.user_context[field].claim
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", claim):
                    raise _NotExported(
                        f"`$user.{field}` reads the claim `{claim}`, not a plain security "
                        "context attribute"
                    )
                self.claims[field] = claim
                return "{ securityContext." + claim + " }"
            if isinstance(node, exp.Literal):
                # Braces would read as a Cube template; masked names inside a string are not
                # what the planner sees.
                plain = not re.search(r"[{}]|__ref_|__user_", node.this)
                if node.is_string and dim.type == "categorical" and plain:
                    return node.this
                # Plain numbers only (no exponent), the ones `cosmos import cube` reads back.
                if not node.is_string and dim.type == "number" and _NUMBER.match(node.this):
                    return node.this
            raise _NotExported(
                f"it compares `{dim.name}` with `{shown(node)}`; only $user, a string for a "
                "categorical dimension and a number for a number dimension are exported"
            )

        def build(node: exp.Expression) -> dict[str, Any]:
            if isinstance(node, exp.Paren):
                return build(node.this)
            if isinstance(node, exp.And | exp.Or):
                key = "and" if isinstance(node, exp.And) else "or"
                items: list[dict[str, Any]] = []
                for part in (node.this, node.expression):
                    sub = build(part)
                    items.extend(sub[key] if set(sub) == {key} else [sub])
                return {key: items}
            if isinstance(node, exp.In) and not any(
                v for k, v in node.args.items() if k not in ("this", "expressions")
            ):
                dim = member(node.this)
                values = [value(v, dim) for v in node.expressions]
                return {"member": dim.name, "operator": "equals", "values": values}
            op = _SQL_OPS.get(type(node))
            if op is None:
                raise _NotExported(
                    f"its condition `{shown(node)}` has no exact Cube filter (=, IN, and <, <=, "
                    ">, >= on numbers, joined by AND and OR; Cube's notEquals and notIn keep "
                    "null rows that SQL drops)"
                )
            left, right = node.this, node.expression
            if isinstance(right, exp.Column) and right.name.startswith("__ref_"):
                left, right, op = right, left, _FLIPPED[op]
            dim = member(left)
            if op != "equals" and dim.type != "number":
                raise _NotExported(f"it orders the {dim.type} dimension `{dim.name}`")
            return {"member": dim.name, "operator": op, "values": [value(right, dim)]}

        return build(tree)

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
            if dim.sensitivity:
                item["meta"] = {"sensitivity": dim.sensitivity}
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
                # Real, as the planner averages: an integer AVG truncates in SQL Server.
                sql = f"1e0 * {sql}" if _IDENT.fullmatch(text) else f"1e0 * ({sql})"
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
        metric = self.graph.metrics.get(name)
        if isinstance(metric, CumulativeMetric):
            # The planner refuses a cumulative metric inside another metric, so its numbers
            # are not defined in Cosmos; Cube must not get a definition of its own.
            raise _NotExported(
                f"it uses the cumulative metric `{name}`, and Cosmos does not compute a "
                "cumulative metric inside another one"
            )
        if name not in self.placed and metric is not None:
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
            sql = f"1e0 * {self._ref(num, cube)} / NULLIF({self._ref(den, cube)}, 0)"
            item = {"name": metric.name, "type": "number", "sql": sql}
            return cube, item, self.leaves[num] | self.leaves[den]
        if isinstance(metric, DerivedMetric):
            return self._derived(metric)
        return self._cumulative(metric)

    def _cumulative(self, metric: CumulativeMetric) -> tuple[str, dict[str, Any], set[str]]:
        """A ``grain_to_date`` running total of a ``sum`` or ``count`` measure: a measure with a
        ``to_date`` rolling window, which sums the rows from the start of the period to the end
        of each time bucket, as the planner's running total does. A ``window: N days`` over a
        ``sum``, ``count``, ``min`` or ``max`` measure (DEC-30): a measure with
        ``rolling_window: {trailing: N day, offset: end}``, which aggregates the rows after the
        end of the bucket minus N days, up to the end of the bucket; at day grain, the days
        D - (N - 1) to D, as the planner's window."""
        if metric.window:
            days = metric.window_days()
            if days is None:
                raise _NotExported(
                    f"it is cumulative over a rolling window (`{metric.window}`) that is not in "
                    "days, and the Cube export carries only windows in days"
                )
            node, measure = self.graph.measure(metric.measure)
            if measure.agg not in WINDOW_AGGS:
                raise _NotExported(
                    "it is a rolling window over a measure that aggregates with "
                    f"`{measure.agg}`, and the Cube export carries rolling windows of `sum`, "
                    "`count`, `min` and `max` measures only"
                )
            item = self._measure(measure, name=metric.name)
            item["rolling_window"] = {"trailing": f"{days} day", "offset": "end"}
            return node.name, item, {node.name}
        if not metric.grain_to_date:
            raise _NotExported(
                "it is cumulative without `grain_to_date`, and the Cube export carries only "
                "running totals from the start of a period"
            )
        node, measure = self.graph.measure(metric.measure)
        if measure.agg not in _TO_DATE_AGGS:
            raise _NotExported(
                f"it is cumulative over a measure that aggregates with `{measure.agg}`, and the Cube export carries "
                "running totals of `sum` and `count` measures only"
            )
        item = self._measure(measure, name=metric.name)
        item["rolling_window"] = {"type": "to_date", "granularity": metric.grain_to_date}
        return node.name, item, {node.name}

    def _derived(self, metric) -> tuple[str, dict[str, Any], set[str]]:
        if metric.inputs:
            raise _NotExported(
                "it reads an input at an earlier period (`offset`), and the Cube export does "
                "not carry offsets yet"
            )
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
            sql = re.sub(
                r"__ref_(\d+)",
                lambda m: self._ref(names[int(m.group(1))], cube),
                real_division(tree).sql(),
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
