"""Importing the dbt Semantic Layer: the latest spec (``semantic_model`` inside ``models``)
and the legacy spec (``semantic_models`` and ``metrics`` with ``type_params``).

Only the YAML is read; the dbt project is never executed. ``ref('x')`` becomes table ``x``
(prefixed with ``schema=`` when given).

Export: the legacy spec, which MetricFlow still reads. One semantic model per model with
entities, dimensions and measures (each measure also a metric, ``create_metric``), simple,
ratio and derived metrics (offset inputs as ``offset_window``), cumulative metrics with a
rolling window in days or weeks (``window`` in ``cumulative_type_params``) and running totals
since the start of a period over ``sum`` and ``count`` measures (``grain_to_date``, with
``period_agg: last``); filters become ``{{ Dimension('e__d') }}``. Division in derived metrics
and AVG are written with a ``1e0 *`` factor, as in the Cube export. ``import_dbt`` reads the
export back with the same numbers in Cosmos. The export fails closed on row filters and
sensitivity, and on metrics it cannot carry (other running totals, filters on a time dimension,
a business rule or ``$user``), unless the caller accepts the loss. Business rules,
the glossary and the business context are not exported yet; each one present is a warning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from cosmos_semantic.compiler import TIME_GRAINS
from cosmos_semantic.errors import QueryError
from cosmos_semantic.expressions import REF_RE, USER_RE
from cosmos_semantic.interchange import ExportResult, ImportResult, read_yaml_files
from cosmos_semantic.interchange.cube import (
    _TO_DATE_AGGS,
    _plural,
    real_division,
    refuse_policy_loss,
    skip_dependents,
)
from cosmos_semantic.spec import (
    CumulativeMetric,
    DerivedMetric,
    Project,
    RatioMetric,
    SimpleMetric,
    combined_window_refusal,
    cumulative_agg_problem,
    offset_grain_problem,
    parse_offset,
    window_parts,
)

_REF_RE = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")
_JINJA_DIM_RE = re.compile(
    r"""\{\{\s*Dimension\(\s*['"]([^'"]+)['"]\s*\)(?:\.[a-z_]+\([^)]*\))*\s*\}\}"""
)
_JINJA_ANY_RE = re.compile(r"\{\{.*?\}\}")
# MetricFlow's `offset_window`: `<count> <granularity>`, plural allowed, each unit as a Cosmos
# offset in days or months (DEC-36); finer or custom granularities have no Cosmos equivalent.
_DBT_UNIT_RE = re.compile(r"(\d+) (day|week|month|quarter|year)s?")
_DBT_UNIT_SIZE = {
    "day": (1, "day"),
    "week": (7, "day"),
    "month": (1, "month"),
    "quarter": (3, "month"),
    "year": (12, "month"),
}
_GRAINS = {"day": 0, "week": 1, "month": 2, "quarter": 3, "year": 4}
_AGG = {
    "sum": "sum",
    "max": "max",
    "min": "min",
    "count": "count",
    "count_distinct": "count_distinct",
    "average": "avg",
    "avg": "avg",
}


class _Builder:
    def __init__(self, schema: str | None) -> None:
        self.schema = schema
        self.models: list[dict[str, Any]] = []
        self.metrics: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.measure_names: set[str] = set()
        # Offset warnings, by metric, given once the metrics Cosmos cannot shift are dropped.
        self.offset_notes: list[tuple[str, str]] = []

    # -- helpers ---------------------------------------------------------

    def table(self, model_ref: str | None, fallback: str) -> str:
        name = fallback
        if model_ref:
            match = _REF_RE.search(model_ref)
            name = match.group(1) if match else model_ref
        return f"{self.schema}.{name}" if self.schema and "." not in name else name

    def measure(self, model: dict, name: str, agg: str, expr: Any, where: str) -> bool:
        if agg == "sum_boolean":
            agg, expr = "sum", f"CASE WHEN {expr or name} THEN 1 ELSE 0 END"
        if agg not in _AGG:
            self.warnings.append(
                f"{where}: aggregation `{agg}` is not supported; measure `{name}` skipped"
            )
            return False
        item: dict[str, Any] = {"name": name, "agg": _AGG[agg]}
        if expr is not None and str(expr).strip() not in ("1", "*") and str(expr) != name:
            item["expr"] = str(expr)
        elif agg != "count" and expr is None:
            pass
        model.setdefault("measures", []).append(item)
        self.measure_names.add(name)
        return True

    # -- formato legado --------------------------------------------------------

    def legacy_model(self, sm: dict[str, Any]) -> None:
        name = sm["name"]
        model: dict[str, Any] = {"name": name, "table": self.table(sm.get("model"), name)}
        if sm.get("description"):
            model["description"] = sm["description"]
        if _meta(sm).get("owner"):
            model["owner"] = str(_meta(sm)["owner"])
        for entity in sm.get("entities") or []:
            self.entity(model, entity, f"semantic_models.{name}")
        for dim in sm.get("dimensions") or []:
            item = {"name": dim["name"], "type": _dim_type(dim.get("type"), _meta(dim))}
            if dim.get("expr") and dim["expr"] != dim["name"]:
                item["expr"] = str(dim["expr"])
            if dim.get("description"):
                item["description"] = dim["description"]
            model.setdefault("dimensions", []).append(item)
        for measure in sm.get("measures") or []:
            if self.measure(
                model,
                measure["name"],
                measure.get("agg", "sum"),
                measure.get("expr"),
                f"semantic_models.{name}",
            ) and measure.get("description"):
                model["measures"][-1]["description"] = measure["description"]
        self.models.append(model)

    def entity(self, model: dict, entity: dict, where: str, column: str | None = None) -> None:
        kind = entity.get("type")
        if kind == "natural":
            self.warnings.append(f"{where}: `natural` entities (SCD) are not supported; skipped")
            return
        name = entity.get("name") or column
        item = {"name": name, "type": kind}
        expr = entity.get("expr") or column
        if expr and expr != name:
            item["expr"] = str(expr)
        model.setdefault("entities", []).append(item)

    def legacy_metric(self, metric: dict[str, Any]) -> None:
        name, kind = metric["name"], metric.get("type")
        params = metric.get("type_params") or {}
        base = _base(metric)
        where = f"metrics.{name}"
        if kind == "simple":
            measure = params.get("measure")
            measure_name, mfilter = _measure_ref(measure)
            flt = _and(metric.get("filter"), mfilter)
            if measure_name == name:
                if flt:
                    self.warnings.append(
                        f"{where}: metric and measure share a name and there is a filter; the metric was "
                        f"imported as `{name}_filtered`"
                    )
                    self.metrics.append(
                        {
                            "name": f"{name}_filtered",
                            "type": "simple",
                            "measure": name,
                            "filter": flt,
                            **base,
                        }
                    )
                return
            item = {"name": name, "type": "simple", "measure": measure_name, **base}
            if flt:
                item["filter"] = flt
            self.metrics.append(item)
        elif kind == "ratio":
            item = {"name": name, "type": "ratio", **base}
            for side in ("numerator", "denominator"):
                ref_name, flt = _measure_ref(params.get(side))
                if flt:
                    helper = f"{name}__{side}"
                    self.metrics.append(
                        {"name": helper, "type": "simple", "measure": ref_name, "filter": flt}
                    )
                    ref_name = helper
                item[side] = ref_name
            if metric.get("filter"):
                item["filter"] = metric["filter"]
            self.metrics.append(item)
        elif kind == "derived":
            self.derived(name, params.get("expr", ""), params.get("metrics") or [], base, where)
        elif kind == "cumulative":
            cparams = params.get("cumulative_type_params") or {}
            measure_name, _ = _measure_ref(params.get("measure"))
            self.cumulative(
                name,
                measure_name,
                params.get("window") or cparams.get("window"),
                params.get("grain_to_date") or cparams.get("grain_to_date"),
                base,
            )
        else:
            self.warnings.append(f"{where}: metrics of type `{kind}` are not supported; skipped")

    # -- formato atual -------------------------------------------------------

    def latest_model(self, model_yml: dict[str, Any]) -> None:
        sm = model_yml.get("semantic_model")
        if not sm:
            return
        sm = {} if sm is True else sm
        if sm.get("enabled") is False:
            return
        name = sm.get("name") or model_yml["name"]
        where = f"models.{model_yml['name']}"
        model: dict[str, Any] = {"name": name, "table": self.table(None, model_yml["name"])}
        if model_yml.get("description"):
            model["description"] = model_yml["description"]
        for column in model_yml.get("columns") or []:
            col = column["name"]
            if isinstance(column.get("entity"), dict):
                self.entity(model, column["entity"], where, column=col)
            dim = column.get("dimension")
            if isinstance(dim, dict):
                item = {"name": dim.get("name") or col, "type": _dim_type(dim.get("type"))}
                if item["name"] != col:
                    item["expr"] = col
                if column.get("description"):
                    item["description"] = column["description"]
                model.setdefault("dimensions", []).append(item)
        if model_yml.get("primary_entity") and not any(
            e["type"] == "primary" for e in model.get("entities", [])
        ):
            self.warnings.append(
                f"{where}: `primary_entity` without a primary column; joins to `{name}` will not be inferred"
            )
        derived = model_yml.get("derived_semantics") or {}
        for dim in derived.get("dimensions") or []:
            item = {"name": dim["name"], "type": _dim_type(dim.get("type"))}
            if dim.get("expr"):
                item["expr"] = str(dim["expr"])
            model.setdefault("dimensions", []).append(item)
        for entity in derived.get("entities") or []:
            self.entity(model, entity, where)
        for metric in model_yml.get("metrics") or []:
            self.model_metric(model, metric, where)
        self.models.append(model)

    def model_metric(self, model: dict, metric: dict, where: str) -> None:
        name = metric["name"]
        if metric.get("type", "simple") != "simple":
            self.top_metric(metric)
            return
        base = _base(metric)
        if metric.get("filter"):
            measure_name = f"{name}__base"
            if self.measure(
                model, measure_name, metric.get("agg", "sum"), metric.get("expr"), where
            ):
                self.metrics.append(
                    {
                        "name": name,
                        "type": "simple",
                        "measure": measure_name,
                        "filter": metric["filter"],
                        **base,
                    }
                )
        elif self.measure(model, name, metric.get("agg", "sum"), metric.get("expr"), where):
            if metric.get("description"):
                model["measures"][-1]["description"] = metric["description"]

    def top_metric(self, metric: dict[str, Any]) -> None:
        name, kind = metric["name"], metric.get("type")
        base = _base(metric)
        where = f"metrics.{name}"
        if kind == "ratio":
            item = {
                "name": name,
                "type": "ratio",
                "numerator": _plain(metric.get("numerator")),
                "denominator": _plain(metric.get("denominator")),
                **base,
            }
            if metric.get("filter"):
                item["filter"] = metric["filter"]
            self.metrics.append(item)
        elif kind == "derived":
            self.derived(
                name, metric.get("expr", ""), metric.get("input_metrics") or [], base, where
            )
        elif kind == "cumulative":
            self.cumulative(
                name,
                _plain(metric.get("input_metric")),
                metric.get("window"),
                metric.get("grain_to_date"),
                base,
            )
        elif kind == "simple" or "type_params" in metric:
            self.legacy_metric(metric)
        else:
            self.warnings.append(f"{where}: metrics of type `{kind}` are not supported; skipped")

    # -- comuns ----------------------------------------------------------------

    def derived(self, name: str, expr: str, inputs: list, base: dict, where: str) -> None:
        inputs = [{"name": i} if isinstance(i, str) else i for i in inputs]
        # MetricFlow offsets first (DEC-36): `offset_window` maps to an offset input, and
        # `offset_to_grain` (the earlier grain's period to date) has no Cosmos equivalent.
        offsets: list[tuple[str, str] | None] = []
        for item in inputs:
            if item.get("offset_to_grain"):
                self.warnings.append(
                    f"{where}: input `{item.get('name')}` uses `offset_to_grain: "
                    f"{item['offset_to_grain']}`, which Cosmos does not compute; metric skipped"
                )
                return
            window = item.get("offset_window")
            offset = _cosmos_offset(window) if window else None
            if window and offset is None:
                self.warnings.append(
                    f"{where}: `offset_window: {window}` is not supported (only a whole number "
                    "of days, weeks, months, quarters or years above zero); metric skipped"
                )
                return
            offsets.append((str(window), offset) if offset else None)
        names_used = [item.get("name") for item in inputs]
        aliases = {}
        shifted: dict[str, str] = {}
        for item, offset in zip(inputs, offsets, strict=True):
            ref = item.get("name")
            key = item.get("alias") or ref
            if item.get("filter"):
                helper = f"{name}__{key}"
                self.metrics.append(
                    {"name": helper, "type": "simple", "measure": ref, "filter": item["filter"]}
                )
                ref = helper
            elif offset and names_used.count(ref) > 1:
                # The expression also reads `ref` another way, which an offset input of the
                # same metric cannot: the shifted read becomes a metric of its own.
                helper = f"{name}__{key}"
                self.metrics.append(
                    {
                        "name": helper,
                        "type": "derived",
                        "expr": "{" + ref + "}",
                        "inputs": [{"metric": ref, "offset": offset[1]}],
                    }
                )
                self.warn_offset(helper, ref, *offset)
                ref, offset = helper, None
            if offset:
                shifted[ref] = offset[1]
                self.warn_offset(name, ref, *offset)
            aliases[key] = ref
        names = sorted(aliases, key=len, reverse=True)
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None
        converted = (
            pattern.sub(lambda m: "{" + aliases[m.group(1)] + "}", expr) if pattern else expr
        )
        item = {"name": name, "type": "derived", "expr": converted, **base}
        if shifted:
            item["inputs"] = [{"metric": r, "offset": o} for r, o in shifted.items()]
        self.metrics.append(item)

    def warn_offset(self, name: str, ref: str, window: str, offset: str) -> None:
        """Where an imported ``offset_window`` gives MetricFlow's numbers, and where not."""
        unit = parse_offset(offset)[1]
        fits = [g for g in _GRAINS if not offset_grain_problem(offset, g)]
        # MetricFlow shifts the aggregated periods when the query's grain is not coarser than
        # the window's unit, and otherwise each day before aggregating, clamping month ends.
        dbt_unit = _DBT_UNIT_RE.fullmatch(window.strip().lower()).group(2)
        coarser = [g for g in fits if unit == "month" and _GRAINS[g] > _GRAINS[dbt_unit]]
        text = (
            f"metrics.{name}: `{ref}` is read `{offset}` earlier (`offset_window: {window}`), "
            f"queried by {' or '.join(fits)}. Cosmos moves the start of the query's period, "
            "returns only periods with facts and applies filters on the time dimension after "
            "the shift; MetricFlow joins a time spine, so it also returns periods without facts "
            "whose earlier period has some"
        )
        if coarser:
            text += (
                f"; by {' or '.join(coarser)} MetricFlow shifts each day before aggregating "
                "and clamps month ends, so its numbers differ there from Cosmos's, which shift "
                "the whole period"
            )
        self.offset_notes.append((name, text))

    def skip_invalid_offsets(self) -> None:
        """Drops the derived metrics whose offset input Cosmos cannot shift: one over a
        cumulative metric or over a metric with offsets of its own, with the metrics that use
        them, each with a warning (DEC-36). The condition is the validator's own."""
        by_name = {m["name"]: m for m in self.metrics}
        skipped: set[str] = set()

        def under(ref: str, seen: set[str]):
            metric = by_name.get(ref)
            if metric is None or ref in seen:
                return
            seen.add(ref)
            yield metric
            refs = [metric.get("numerator"), metric.get("denominator")]
            refs += REF_RE.findall(metric.get("expr") or "")
            for child in refs:
                if child:
                    yield from under(child.split(".")[-1], seen)

        for metric in self.metrics:
            for item in metric.get("inputs") or []:
                for found in under(item["metric"], set()):
                    if found["type"] == "cumulative":
                        reason = f"it uses the cumulative metric `{found['name']}`"
                    elif found["type"] == "derived" and found.get("inputs"):
                        reason = f"it uses `{found['name']}`, which has offsets of its own"
                    else:
                        continue
                    self.warnings.append(
                        f"metrics.{metric['name']}: `{item['metric']}` is read with an offset, "
                        f"and {reason}, which Cosmos does not shift; metric skipped"
                    )
                    skipped.add(metric["name"])
                    break
        kept = [m for m in self.metrics if m["name"] not in skipped]
        self.metrics = skip_dependents(kept, skipped, self.warnings, lambda m: f"metrics.{m}")
        names = {m["name"] for m in self.metrics}
        self.warnings += [text for name, text in self.offset_notes if name in names]

    def cumulative(self, name, measure, window, grain_to_date, base) -> None:
        item = {"name": name, "type": "cumulative", "measure": measure, **base}
        if grain_to_date:
            item["grain_to_date"] = grain_to_date
        elif window:
            item["window"] = str(window)
            self.warnings.append(
                f"metrics.{name}: rolling window `{window}`: only windows in days or weeks, "
                "queried by day, are supported, and only days with data appear (MetricFlow "
                "uses a time spine)"
            )
        self.metrics.append(item)

    def skip_invalid_cumulative(self) -> None:
        """Drops the cumulative metrics model validation refuses: a running total, to-date or
        not, or a rolling window over a measure whose per-period values do not combine (`avg`,
        `count_distinct`), and the metrics that use them, each with a warning (DEC-32). The
        condition is the validator's own (``cumulative_agg_problem``), and for windows
        ``combined_window_refusal``: a `count_distinct` (DEC-49) or `avg` (DEC-60) window is
        valid in Cosmos but not imported until MetricFlow's numbers are checked against the
        engine's."""
        aggs = {m["name"]: m["agg"] for model in self.models for m in model.get("measures", [])}
        skipped: set[str] = set()
        for metric in self.metrics:
            if metric["type"] != "cumulative" or metric["measure"] not in aggs:
                continue
            agg = aggs[metric["measure"]]
            spec = CumulativeMetric.model_validate(metric)
            problem = (
                combined_window_refusal(agg) if spec.window else cumulative_agg_problem(spec, agg)
            )
            if problem:
                self.warnings.append(
                    f"metrics.{metric['name']}: over measure `{metric['measure']}`: {problem}; "
                    "metric skipped"
                )
                skipped.add(metric["name"])
        kept = [m for m in self.metrics if m["name"] not in skipped]
        self.metrics = skip_dependents(kept, skipped, self.warnings, lambda m: f"metrics.{m}")

    # -- filtros Jinja do MetricFlow -----------------------------------------

    def convert_filters(self) -> None:
        entity_owner = {}
        for model in self.models:
            for entity in model.get("entities", []):
                if entity["type"] in ("primary", "unique"):
                    entity_owner[entity["name"]] = model["name"]

        def convert(text: str, where: str) -> str:
            def dim(match: re.Match[str]) -> str:
                ref = match.group(1)
                if "__" in ref:
                    entity, dim_name = ref.split("__", 1)
                    model = entity_owner.get(entity)
                    if model:
                        return "{" + f"{model}.{dim_name}" + "}"
                    self.warnings.append(f"{where}: filter entity `{entity}` not found")
                    return "{" + dim_name + "}"
                return "{" + ref + "}"

            out = _JINJA_DIM_RE.sub(dim, text)
            if _JINJA_ANY_RE.search(out):
                self.warnings.append(
                    f"{where}: filter `{text}` uses Jinja other than Dimension(...); review it"
                )
            return out

        for metric in self.metrics:
            if metric.get("filter"):
                metric["filter"] = convert(metric["filter"], f"metrics.{metric['name']}")
        for model in self.models:
            for flt in model.get("row_filters", []):
                flt["filter"] = convert(flt["filter"], f"semantic_models.{model['name']}")


def import_dbt(path: str | Path, *, schema: str | None = None) -> ImportResult:
    builder = _Builder(schema)
    for file, text in read_yaml_files(path):
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            builder.warnings.append(f"{file}: invalid YAML ({exc}); skipped")
            continue
        if not isinstance(data, dict):
            continue
        for sm in data.get("semantic_models") or []:
            builder.legacy_model(sm)
        for model_yml in data.get("models") or []:
            if isinstance(model_yml, dict):
                builder.latest_model(model_yml)
        for metric in data.get("metrics") or []:
            if "type_params" in metric:
                builder.legacy_metric(metric)
            else:
                builder.top_metric(metric)
    builder.convert_filters()
    builder.skip_invalid_cumulative()
    builder.skip_invalid_offsets()
    if not builder.models:
        builder.warnings.append("no semantic model found")
    if not schema:
        builder.warnings.append("tables come from ref(); check `table` or use --schema")
    project = Project.model_validate(
        {"version": 1, "semantic_models": builder.models, "metrics": builder.metrics}
    )
    return ImportResult(project, builder.warnings)


def _cosmos_offset(window: Any) -> str | None:
    """The Cosmos offset of a MetricFlow ``offset_window`` (``1 week`` is ``7 days``, ``1 year``
    is ``12 months``); None when it does not map."""
    match = _DBT_UNIT_RE.fullmatch(str(window).strip().lower())
    if not match or int(match.group(1)) == 0:
        return None
    size, unit = _DBT_UNIT_SIZE[match.group(2)]
    amount = int(match.group(1)) * size
    return f"{amount} {unit}" if amount == 1 else f"{amount} {unit}s"


def _dim_type(kind: str | None, meta: dict | None = None) -> str:
    out = {"time": "time", "categorical": "categorical"}.get(kind or "categorical", "categorical")
    # `export_dbt` keeps the Cosmos types dbt lacks in `meta`.
    if out == "categorical" and (meta or {}).get("cosmos_type") in ("number", "boolean"):
        return meta["cosmos_type"]
    return out


def _meta(item: dict) -> dict:
    """``config.meta`` (or the older top-level ``meta``) of a dbt node."""
    config = item.get("config") if isinstance(item.get("config"), dict) else {}
    meta = config.get("meta") or item.get("meta")
    return meta if isinstance(meta, dict) else {}


def _base(metric: dict) -> dict[str, Any]:
    base = {k: metric[k] for k in ("description", "label") if metric.get(k)}
    if _meta(metric).get("owner"):
        base["owner"] = str(_meta(metric)["owner"])
    return base


def _measure_ref(value: Any) -> tuple[str, str | None]:
    if isinstance(value, dict):
        return value["name"], value.get("filter")
    return str(value), None


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        return value["name"]
    return str(value)


def _and(a: str | None, b: str | None) -> str | None:
    if a and b:
        return f"({a}) AND ({b})"
    return a or b


# -- export -----------------------------------------------------------------------------------

_DBT_AGG = {
    "count": "count",
    "count_distinct": "count_distinct",
    "sum": "sum",
    "avg": "average",
    "min": "min",
    "max": "max",
}
# Members of the authoring format that a later slice maps to dbt.
_DBT_LATER = ("business_rules", "glossary", "teams", "systems", "processes")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _NotExported(Exception):
    """A metric the dbt export cannot carry; the message is the reason."""


def export_dbt(
    project: Project,
    *,
    allow_policy_loss: bool = False,
    allow_metric_loss: bool = False,
) -> ExportResult:
    """Writes the project as dbt Semantic Layer (MetricFlow) YAML, with a warning for each
    member this slice does not export. Refuses, unless ``allow_policy_loss``, a project with
    row filters or sensitivity, and, unless ``allow_metric_loss``, one with metrics that
    ``import_dbt`` cannot read back or that MetricFlow would compute differently."""
    from cosmos_semantic.compiler import compile_project
    from cosmos_semantic.errors import MetricLossError

    graph = compile_project(project)  # only valid models are exported
    warnings: list[str] = []
    refuse_policy_loss(project, "dbt", allow_policy_loss, warnings)

    writer = _DbtWriter(graph, warnings)
    if writer.lost and not allow_metric_loss:
        reasons = "; ".join(f"{k}: {v}" for k, v in writer.lost.items())
        raise MetricLossError(
            f"{_plural(len(writer.lost), 'metric')} ({', '.join(writer.lost)}) would NOT be "
            f"exported ({reasons}); the dbt model would not have them. Pass "
            "--allow-metric-loss to export anyway"
        )
    for name, reason in writer.lost.items():
        warnings.append(f"metrics.{name}: {reason}; it is not exported to dbt")
    models = writer.semantic_models()

    if project.user_context:
        warnings.append(
            f"user_context: {', '.join(project.user_context)} not exported to dbt (only row "
            "filters use it)"
        )
    if project.timezone:
        warnings.append(
            "timezone: not exported to dbt; MetricFlow has no model time zone, so relative "
            f"dates such as today follow the warehouse session, not {project.timezone}"
        )
    for key in _DBT_LATER:
        items = getattr(project, key)
        if items:
            warnings.append(f"{key}: not exported to dbt yet ({len(items)} in the project)")
    if project.custom_extensions:
        warnings.append(
            f"custom_extensions: {', '.join(project.custom_extensions)} not exported to dbt yet"
        )
    doc: dict[str, Any] = {"semantic_models": models}
    if writer.metrics:
        doc["metrics"] = writer.metrics
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    return ExportResult(text, list(dict.fromkeys(warnings)))


class _DbtWriter:
    """Builds the semantic models and metrics from a compiled graph. Every measure is a dbt
    metric too (``create_metric``), since the engine lets metrics use measures directly."""

    def __init__(self, graph, warnings: list[str]) -> None:
        self.graph = graph
        self.warnings = warnings
        self.lost: dict[str, str] = {}
        self.metrics: list[dict[str, Any]] = []
        self.done: set[str] = set(graph.measure_owner)  # names dbt has as metrics
        for metric in graph.project.metrics:
            self._place(metric.name)

    # -- semantic models --------------------------------------------------------------------

    def semantic_models(self) -> list[dict[str, Any]]:
        out = []
        schemas: dict[str, list[str]] = {}
        for node in self.graph.models.values():
            spec = node.spec
            where = f"semantic_models.{spec.name}"
            schema, _, table = spec.table.rpartition(".")
            if schema:
                schemas.setdefault(schema, []).append(spec.name)
            item: dict[str, Any] = {"name": spec.name}
            if spec.description:
                item["description"] = spec.description
            item["model"] = f"ref('{table}')"
            times = [d.name for d in spec.dimensions if d.type == "time"]
            if spec.measures and times:
                item["defaults"] = {"agg_time_dimension": times[0]}
            elif spec.measures:
                self.warnings.append(
                    f"{where}: it has measures but no time dimension; MetricFlow needs an "
                    "agg_time_dimension for them, so add one in dbt"
                )
            if spec.data_source:
                self.warnings.append(
                    f"{where}: data_source `{spec.data_source}` not exported (a dbt project "
                    "reads one warehouse)"
                )
            if spec.dimensions and not any(e.type == "primary" for e in spec.entities):
                self.warnings.append(
                    f"{where}: it has dimensions but no primary entity, which MetricFlow needs"
                )
            if spec.entities:
                item["entities"] = [_dbt_entity(e) for e in spec.entities]
            if spec.dimensions:
                item["dimensions"] = [_dbt_dimension(d) for d in spec.dimensions]
            if spec.measures:
                item["measures"] = [_dbt_measure(m) for m in spec.measures]
            if spec.owner:
                item["config"] = {"meta": {"owner": spec.owner}}
            out.append(item)
        if len(schemas) == 1:
            schema = next(iter(schemas))
            self.warnings.append(
                "semantic_models: tables are written as ref() to the dbt model of the same "
                f"name, without their schema `{schema}`; import with "
                f"`cosmos import dbt --schema {schema}`"
            )
        else:
            for schema, names in schemas.items():
                self.warnings.append(
                    f"semantic_models: {', '.join(names)} are written as ref() without their "
                    f"schema `{schema}`, which `cosmos import dbt` does not read back"
                )
        return out

    # -- metrics ----------------------------------------------------------------------------

    def _use(self, ref: str) -> str:
        """A member a metric references, placed first when it is a metric."""
        name = ref.split(".")[-1]
        if isinstance(self.graph.metrics.get(name), CumulativeMetric):
            # The planner refuses a cumulative metric inside another metric, so its numbers
            # are not defined in Cosmos; MetricFlow must not get a definition of its own.
            raise _NotExported(
                f"it uses the cumulative metric `{name}`, and Cosmos does not compute a "
                "cumulative metric inside another one"
            )
        if name in self.graph.metrics:
            self._place(name)
        if name not in self.done:
            raise _NotExported(f"it uses `{name}`, which is not exported")
        metric = self.graph.metrics.get(name)
        if isinstance(metric, SimpleMetric) and metric.filter:
            # MetricFlow filters each input in its own subquery and joins them, so a group
            # with no matching rows is NULL there; the engine counts it as 0.
            if self.graph.measure(metric.measure)[1].agg in ("count", "count_distinct"):
                raise _NotExported(
                    f"it uses `{name}`, a filtered count, which MetricFlow returns as NULL "
                    "where the engine returns 0"
                )
        return name

    def _place(self, name: str) -> None:
        if name in self.done or name in self.lost:
            return
        metric = self.graph.metrics[name]
        try:
            body = self._metric(metric)
        except _NotExported as exc:
            self.lost[name] = str(exc)
            return
        # dbt requires a label.
        item: dict[str, Any] = {"name": name, "label": metric.label or name}
        if metric.description:
            item["description"] = metric.description
        item.update(body)
        if metric.owner:
            item["config"] = {"meta": {"owner": metric.owner}}
        self.done.add(name)
        self.metrics.append(item)

    def _metric(self, metric) -> dict[str, Any]:
        if isinstance(metric, SimpleMetric):
            item: dict[str, Any] = {"type": "simple", "type_params": {"measure": metric.measure}}
        elif isinstance(metric, RatioMetric):
            params = {"numerator": self._use(metric.numerator)}
            params["denominator"] = self._use(metric.denominator)
            item = {"type": "ratio", "type_params": params}
            if metric.filter:
                self._check_ratio_filter(metric)
        elif isinstance(metric, DerivedMetric):
            return self._derived(metric)
        else:
            return self._cumulative(metric)
        if metric.filter:
            item["filter"] = self._filter(metric.filter, self._leaves(metric.name))
        return item

    def _cumulative(self, metric: CumulativeMetric) -> dict[str, Any]:
        """A rolling window in days or weeks: MetricFlow's ``window`` covers the days
        D - (N - 1) to D of ``metric_time`` (7N days for N weeks), as the planner's does.
        Running totals since the start of a period are ``_to_date``."""
        if metric.grain_to_date:
            return self._to_date(metric)
        if not metric.window:
            raise _NotExported(
                "it is a running total without a window, and the dbt export does not carry it yet"
            )
        parts = window_parts(metric.window)
        if parts is None:
            raise _NotExported(
                f"its window `{metric.window}` is not in days or weeks, the only units the "
                "engine computes"
            )
        node, measure = self.graph.measure(metric.measure)
        problem = combined_window_refusal(measure.agg)
        if problem:
            raise _NotExported(problem)
        owner, spec = node.spec.name, node.spec
        times = [d.name for d in spec.dimensions if d.type == "time"]
        if not times:
            raise _NotExported(
                f"its measure's model `{owner}` has no time dimension for MetricFlow to window over"
            )
        others = ", ".join(f"`{owner}.{t}`" for t in times[1:])
        self.warnings.append(
            f"metrics.{metric.name}: MetricFlow windows over `metric_time`, which is "
            f"`{owner}.{times[0]}`; by that dimension at day grain the numbers are the engine's on "
            "the days with facts. MetricFlow also returns days without facts inside a window and "
            "coarser grains (the engine refuses them)"
            + (
                f"; queried by {others}, the engine windows over that dimension instead, which "
                "MetricFlow does not"
                if others
                else ""
            )
        )
        # A window in weeks stays in weeks (DEC-27): MetricFlow subtracts the interval, so
        # `2 weeks` is the 14 days `14 days` would be.
        count, unit = parts
        window = f"{count} {unit}" if count == 1 else f"{count} {unit}s"
        params = {"measure": metric.measure, "cumulative_type_params": {"window": window}}
        return {"type": "cumulative", "type_params": params}

    def _to_date(self, metric: CumulativeMetric) -> dict[str, Any]:
        """A ``grain_to_date`` running total of a ``sum`` or ``count`` measure (DEC-31).

        MetricFlow joins each day of its time spine to the facts from the start of that day's
        period (``date_trunc``) up to the day, both included: by day, the planner's running sum
        of the daily values. At a coarser query grain MetricFlow computes by day and keeps one
        value per bucket with ``period_agg``; ``last`` keeps the bucket's last day, the total
        from the start of the period to the end of the bucket, which is the planner's running
        sum of per-bucket values. MetricFlow's default, ``first``, keeps the first day."""
        node, measure = self.graph.measure(metric.measure)
        if measure.agg not in _TO_DATE_AGGS:
            raise _NotExported(
                f"it is a running total over a measure that aggregates with `{measure.agg}`, "
                "and the dbt export carries running totals of `sum` and `count` measures only"
            )
        owner, spec = node.spec.name, node.spec
        times = [d.name for d in spec.dimensions if d.type == "time"]
        if not times:
            raise _NotExported(
                f"its measure's model `{owner}` has no time dimension for MetricFlow to "
                "accumulate over"
            )
        period = metric.grain_to_date
        # The grains the planner accepts (``to_date_grain_problem``), hour aside: dbt groups
        # the exported time dimensions by day at the finest.
        fits = [
            g
            for g in TIME_GRAINS[1 : TIME_GRAINS.index(period) + 1]
            if g != "week" or period == "week"
        ]
        grains = fits[0] if len(fits) == 1 else f"{', '.join(fits[:-1])} or {fits[-1]}"
        others = ", ".join(f"`{owner}.{t}`" for t in times[1:])
        self.warnings.append(
            f"metrics.{metric.name}: MetricFlow accumulates over `metric_time`, which is "
            f"`{owner}.{times[0]}`; by that dimension at {grains} grain the numbers are the "
            "engine's on the periods with facts. MetricFlow also returns periods without facts "
            f"after the first fact of a {period} and the grains the engine refuses"
            + (
                f"; queried by {others}, the engine accumulates over that dimension instead, "
                "which MetricFlow does not"
                if others
                else ""
            )
        )
        params = {
            "measure": metric.measure,
            "cumulative_type_params": {"grain_to_date": period, "period_agg": "last"},
        }
        return {"type": "cumulative", "type_params": params}

    def _derived(self, metric) -> dict[str, Any]:
        refs: list[str] = []

        def mask(match):
            refs.append(match.group(1))
            return f"__ref_{len(refs) - 1}"

        tree = sqlglot.parse_one(REF_RE.sub(mask, metric.expr))
        bare = [c.sql() for c in tree.find_all(exp.Column) if not c.name.startswith("__ref_")]
        if bare:
            raise _NotExported(
                f"its expression uses bare columns ({', '.join(bare)}), and a dbt derived "
                "metric references metrics only"
            )
        # An alias (DEC-44) stays the name in the expression, as MetricFlow's input `alias`.
        aliases = metric.aliases()
        names = [ref if ref in aliases else self._use(ref) for ref in refs]
        used = {alias: self._use(target) for alias, target in aliases.items()}
        if tree.find(exp.Div):
            # Division as the planner computes it: real, and null on a zero denominator.
            text = re.sub(
                r"__ref_(\d+)", lambda m: names[int(m.group(1))], real_division(tree).sql()
            )
        else:
            it = iter(names)
            text = REF_RE.sub(lambda m: next(it), metric.expr)
        # An input's name is unqualified (`OffsetInput.metric`), as dbt names it.
        windows = {ref: _dbt_offset_window(offset) for ref, offset in metric.offsets().items()}
        inputs = []
        for name in dict.fromkeys(names):
            item: dict[str, Any] = {"name": used.get(name, name)}
            if name in windows:
                item["offset_window"] = windows[name]
            if name in aliases:
                item["alias"] = name
            inputs.append(item)
        if windows:
            self._warn_offsets(metric, windows)
        params = {"expr": text, "metrics": inputs}
        return {"type": "derived", "type_params": params}

    def _warn_offsets(self, metric: DerivedMetric, windows: dict[str, str]) -> None:
        """An offset as MetricFlow's ``offset_window`` (DEC-36): one warning naming
        ``metric_time`` and where MetricFlow's numbers differ from the engine's."""
        times = []
        for ref in windows:
            for owner in sorted(self._leaves(metric.target(ref))):
                spec = self.graph.models[owner].spec
                first = next((d.name for d in spec.dimensions if d.type == "time"), None)
                if first is None:
                    raise _NotExported(
                        f"it reads `{ref}` at an earlier period, and its model `{owner}` has no "
                        "time dimension for MetricFlow to shift"
                    )
                times.append(f"`{owner}.{first}`")
        times = list(dict.fromkeys(times))
        offsets = metric.offsets()
        aliases = metric.aliases()
        reads = ", ".join(
            f"`{metric.target(ref)}`"
            + (f" (as `{ref}`)" if ref in aliases else "")
            + f" by `{offsets[ref]}` (`offset_window: {window}`)"
            for ref, window in windows.items()
        )
        # The grains the engine accepts for every offset of the metric.
        grains = [
            g for g in _GRAINS if all(not offset_grain_problem(o, g) for o in offsets.values())
        ]
        which = "that dimension" if len(times) == 1 else "those dimensions"
        self.warnings.append(
            f"metrics.{metric.name}: MetricFlow shifts {reads} on `metric_time`, which is "
            f"{', '.join(times)}; by {which} at the grains the engine accepts "
            f"({', '.join(grains)}) the periods both return have the same numbers. MetricFlow "
            "joins a time spine, so it also returns periods without facts whose earlier period "
            "has some, and grains the engine refuses. Both apply a filter on that time "
            "dimension after the shift, so the first period of a range reads the period before "
            "it (the engine filters the facts first for a shape it cannot split, such as "
            "`NOT`); other filters restrict the facts first in both"
        )

    def _measures(self, name: str) -> set[str]:
        """The measures a metric aggregates, through nested ratio and derived metrics."""
        if name in self.graph.measure_owner:
            return {name}
        metric = self.graph.metrics[name]
        if isinstance(metric, RatioMetric):
            return self._measures(metric.numerator) | self._measures(metric.denominator)
        if isinstance(metric, DerivedMetric):
            refs = [metric.target(r) for r in REF_RE.findall(metric.expr)]
            return set().union(*(self._measures(r.split(".")[-1]) for r in refs))
        return {metric.measure}

    def _leaves(self, name: str) -> set[str]:
        """The models whose measures a metric aggregates."""
        return {self.graph.measure_owner[m] for m in self._measures(name)}

    def _check_ratio_filter(self, metric) -> None:
        """The engine applies a ratio's filter inside every input's aggregation, so a count
        input becomes a filtered count: 0 where MetricFlow's WHERE and join give NULL. Only a
        ratio of two plain inputs on one model, computed in one MetricFlow query, is safe."""
        measures = self._measures(metric.name)
        counts = sorted(
            m for m in measures if self.graph.measure(m)[1].agg in ("count", "count_distinct")
        )
        nested = any(
            isinstance(self.graph.metrics.get(side), RatioMetric | DerivedMetric)
            for side in (metric.numerator, metric.denominator)
        )
        if counts and (len(self._leaves(metric.name)) > 1 or nested):
            raise _NotExported(
                f"its filter turns the count `{counts[0]}` into a filtered count, which "
                "MetricFlow returns as NULL where the engine returns 0"
            )

    def _filter(self, flt: str, models: set[str]) -> str:
        """A metric filter as MetricFlow Jinja: ``{{ Dimension('entity__dimension') }}``."""
        if USER_RE.search(flt):
            raise _NotExported("its filter uses $user, which only Cosmos resolves")

        def ref(match):
            text = match.group(1)
            found = set()
            for model in sorted(models):
                try:
                    found.add(self.graph.resolve_dimension(text, context=model))
                except QueryError as exc:
                    raise _NotExported(
                        f"its filter reference `{text}` does not resolve ({exc})"
                    ) from None
            if len(found) > 1:
                raise _NotExported(
                    f"its filter reference `{text}` names a different dimension for each "
                    "measure it filters"
                )
            dim = found.pop()
            spec = self.graph.models[dim.model].spec
            own = {d.name: d for d in spec.dimensions}
            if dim.name not in own:
                raise _NotExported(f"its filter uses the business rule `{text}`, not exported yet")
            if own[dim.name].type == "time":
                raise _NotExported(
                    f"its filter uses the time dimension `{text}`, which MetricFlow compares "
                    "truncated to its time_granularity"
                )
            entity = next((e.name for e in spec.entities if e.type == "primary"), None)
            if entity is None:
                raise _NotExported(
                    f"its filter uses `{text}`, and its model `{dim.model}` has no primary "
                    "entity to name it by"
                )
            return "{{ Dimension('" + f"{entity}__{dim.name}" + "') }}"

        return REF_RE.sub(ref, flt)


def _dbt_offset_window(offset: str) -> str:
    """A Cosmos offset as MetricFlow's ``offset_window``, in the largest unit that holds it a
    whole number of times (``12 months`` is ``1 year``, ``14 days`` is ``2 weeks``). MetricFlow
    shifts the aggregated periods when the query's grain is not coarser than that unit, and
    every grain the engine accepts for the offset is not, so both move the period start. This
    is MetricFlow 0.210 and later; earlier versions shift each day before aggregating."""
    amount, unit = parse_offset(offset)
    for name in ("year", "quarter", "month", "week", "day"):
        size, base = _DBT_UNIT_SIZE[name]
        if base == unit and amount % size == 0:
            count = amount // size
            return f"{count} {name}" if count == 1 else f"{count} {name}s"
    raise AssertionError(offset)  # a day or a month always divides it


def _dbt_entity(entity) -> dict[str, Any]:
    item: dict[str, Any] = {"name": entity.name, "type": entity.type}
    if entity.expr and entity.expr != entity.name:
        item["expr"] = entity.expr
    return item


def _dbt_dimension(dim) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": dim.name,
        "type": "time" if dim.type == "time" else "categorical",
    }
    if dim.expr:
        item["expr"] = dim.expr
    if dim.description:
        item["description"] = dim.description
    if dim.type == "time":
        # The model does not say how fine a time column is, so every time dimension is written
        # as day: dbt groups and filters it by day, never by hour as the engine can.
        item["type_params"] = {"time_granularity": "day"}
    elif dim.type != "categorical":
        # dbt has only categorical and time dimensions; `import_dbt` reads the type back.
        item["config"] = {"meta": {"cosmos_type": dim.type}}
    return item


def _dbt_measure(measure) -> dict[str, Any]:
    item: dict[str, Any] = {"name": measure.name, "agg": _DBT_AGG[measure.agg]}
    if measure.description:
        item["description"] = measure.description
    text = measure.expr
    if measure.agg == "count" and text is None:
        text = "1"  # COUNT(*): dbt would count the column named like the measure
    elif measure.agg == "avg":
        # Real, as the planner averages: an integer AVG truncates in SQL Server.
        text = text or measure.name
        text = f"1e0 * {text}" if _IDENT.fullmatch(text) else f"1e0 * ({text})"
    if text is not None:
        item["expr"] = text
    item["create_metric"] = True
    return item
