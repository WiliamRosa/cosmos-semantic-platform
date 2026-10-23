"""Importação do Apache Ossie (antigo Open Semantic Interchange), versões 0.1.x e 0.2.

- ``datasets`` viram modelos; ``fields`` com ``dimension`` viram dimensões.
- ``relationships`` viram entidades (primária no destino, estrangeira na origem).
- Métricas: agregações simples viram medidas; expressões compostas viram uma medida por
  agregação mais uma métrica derivada, inclusive entre datasets.
- ``ai_context.synonyms`` vira glossário, para que agentes encontrem o membro por sinônimo.
- ``custom_extensions`` do próprio Cosmos restauram o modelo sem perdas; as de outros
  fornecedores ficam em ``custom_extensions``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from cosmos_semantic.interchange import ImportResult, read_yaml_files
from cosmos_semantic.spec import Project

VENDOR = "COSMOS_SEMANTIC"
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
                warnings.append(f"{where}: expressão só no dialeto {name}; revise a portabilidade")
            return dialects[name]
    if dialects:
        name, text = next(iter(dialects.items()))
        warnings.append(f"{where}: dialeto {name} é opaco no Ossie; expressão usada como está")
        return text
    return None


def _ident(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_") or "x"


def import_ossie(path: str | Path) -> ImportResult:
    files = read_yaml_files(path)
    if len(files) != 1:
        raise ValueError("informe um único documento Ossie (YAML ou JSON)")
    _, text = files[0]
    doc = yaml.safe_load(text)
    if isinstance(doc.get("semantic_model"), list):
        doc = doc["semantic_model"][0]
    warnings: list[str] = []

    for ext in doc.get("custom_extensions") or []:
        if ext.get("vendor_name") == VENDOR:
            data = json.loads(ext["data"])
            if "project" in data:
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
            warnings.append(f"{where}: dataset inexistente; ignorado")
            continue
        if len(rel["from_columns"]) != 1:
            warnings.append(f"{where}: chaves compostas não são suportadas; relação ignorada")
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


def _key(model, entity_name, columns, exprs, kind, where, warnings) -> None:
    if len(columns) != 1:
        warnings.append(f"{where}: chave composta {columns} não é suportada; ignorada")
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
        warnings.append(f"{where}: sem expressão; ignorada")
        return
    try:
        tree = sqlglot.parse_one(text)
    except sqlglot.errors.ParseError as exc:
        warnings.append(f"{where}: expressão inválida ({exc}); ignorada")
        return

    def dataset_of(column: exp.Column) -> str | None:
        if column.table:
            return column.table if column.table in models else None
        return scope

    aggregates = [n for n in tree.find_all(*_AGGS) if not n.find_ancestor(*_AGGS)]
    if not aggregates:
        warnings.append(f"{where}: a expressão não tem agregação; ignorada")
        return
    simple = len(aggregates) == 1 and tree is aggregates[0]
    replacements: dict[int, str] = {}
    for i, agg in enumerate(aggregates):
        columns = list(agg.find_all(exp.Column))
        owners = {dataset_of(c) for c in columns} or {scope}
        if None in owners or len(owners) != 1:
            warnings.append(f"{where}: agregação `{agg.sql()}` sem dataset único; métrica ignorada")
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
