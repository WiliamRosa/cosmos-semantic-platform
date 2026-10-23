"""Importação do dbt Semantic Layer: formato atual (``semantic_model`` dentro de ``models``)
e formato legado (``semantic_models`` e ``metrics`` com ``type_params``).

Lê só o YAML, sem executar o projeto dbt. ``ref('x')`` vira a tabela ``x`` (com o esquema
de ``schema=``, se informado).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from cosmos_semantic.interchange import ImportResult, read_yaml_files
from cosmos_semantic.spec import Project

_REF_RE = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")
_JINJA_DIM_RE = re.compile(
    r"""\{\{\s*Dimension\(\s*['"]([^'"]+)['"]\s*\)(?:\.[a-z_]+\([^)]*\))*\s*\}\}"""
)
_JINJA_ANY_RE = re.compile(r"\{\{.*?\}\}")
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

    # -- utilitários ---------------------------------------------------------

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
                f"{where}: agregação `{agg}` não suportada; medida `{name}` ignorada"
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
        for entity in sm.get("entities") or []:
            self.entity(model, entity, f"semantic_models.{name}")
        for dim in sm.get("dimensions") or []:
            item = {"name": dim["name"], "type": _dim_type(dim.get("type"))}
            if dim.get("expr") and dim["expr"] != dim["name"]:
                item["expr"] = str(dim["expr"])
            if dim.get("description"):
                item["description"] = dim["description"]
            model.setdefault("dimensions", []).append(item)
        for measure in sm.get("measures") or []:
            self.measure(
                model,
                measure["name"],
                measure.get("agg", "sum"),
                measure.get("expr"),
                f"semantic_models.{name}",
            )
        self.models.append(model)

    def entity(self, model: dict, entity: dict, where: str, column: str | None = None) -> None:
        kind = entity.get("type")
        if kind == "natural":
            self.warnings.append(f"{where}: entidade `natural` (SCD) não é suportada; ignorada")
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
        base = {k: metric[k] for k in ("description", "label") if metric.get(k)}
        where = f"metrics.{name}"
        if kind == "simple":
            measure = params.get("measure")
            measure_name, mfilter = _measure_ref(measure)
            flt = _and(metric.get("filter"), mfilter)
            if measure_name == name:
                if flt:
                    self.warnings.append(
                        f"{where}: métrica e medida têm o mesmo nome e há filtro; a métrica foi "
                        f"importada como `{name}_filtrada`"
                    )
                    self.metrics.append(
                        {
                            "name": f"{name}_filtrada",
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
            self.warnings.append(f"{where}: métricas do tipo `{kind}` não são suportadas; ignorada")

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
                f"{where}: `primary_entity` sem coluna primária; joins para `{name}` não serão inferidos"
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
        base = {k: metric[k] for k in ("description", "label") if metric.get(k)}
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
        base = {k: metric[k] for k in ("description", "label") if metric.get(k)}
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
            self.warnings.append(f"{where}: métricas do tipo `{kind}` não são suportadas; ignorada")

    # -- comuns ----------------------------------------------------------------

    def derived(self, name: str, expr: str, inputs: list, base: dict, where: str) -> None:
        aliases = {}
        for item in inputs:
            ref = item if isinstance(item, str) else item.get("name")
            if isinstance(item, dict) and (
                item.get("offset_window") or item.get("offset_to_grain")
            ):
                self.warnings.append(f"{where}: `offset_window` não é suportado; métrica ignorada")
                return
            if isinstance(item, dict) and item.get("filter"):
                helper = f"{name}__{item.get('alias') or ref}"
                self.metrics.append(
                    {"name": helper, "type": "simple", "measure": ref, "filter": item["filter"]}
                )
                ref = helper
            aliases[(item.get("alias") if isinstance(item, dict) else None) or ref] = ref
        names = sorted(aliases, key=len, reverse=True)
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None
        converted = (
            pattern.sub(lambda m: "{" + aliases[m.group(1)] + "}", expr) if pattern else expr
        )
        self.metrics.append({"name": name, "type": "derived", "expr": converted, **base})

    def cumulative(self, name, measure, window, grain_to_date, base) -> None:
        item = {"name": name, "type": "cumulative", "measure": measure, **base}
        if grain_to_date:
            item["grain_to_date"] = grain_to_date
        elif window:
            item["window"] = str(window)
            self.warnings.append(f"metrics.{name}: janela móvel `{window}` fica fora do MVP")
        self.metrics.append(item)

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
                    self.warnings.append(f"{where}: entidade `{entity}` do filtro não encontrada")
                    return "{" + dim_name + "}"
                return "{" + ref + "}"

            out = _JINJA_DIM_RE.sub(dim, text)
            if _JINJA_ANY_RE.search(out):
                self.warnings.append(
                    f"{where}: o filtro `{text}` usa Jinja que não é Dimension(...); revise"
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
            builder.warnings.append(f"{file}: YAML inválido ({exc}); ignorado")
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
    if not builder.models:
        builder.warnings.append("nenhum semantic model encontrado")
    if not schema:
        builder.warnings.append("as tabelas vieram de ref(); confira `table` ou use --schema")
    project = Project.model_validate(
        {"version": 1, "semantic_models": builder.models, "metrics": builder.metrics}
    )
    return ImportResult(project, builder.warnings)


def _dim_type(kind: str | None) -> str:
    return {"time": "time", "categorical": "categorical"}.get(kind or "categorical", "categorical")


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
