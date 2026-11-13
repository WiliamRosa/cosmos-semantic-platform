"""Importers: the imported model returns the same numbers as the native model on the same data."""

from __future__ import annotations

import datetime as dt
import json
import math
import types
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from cosmos_semantic.cli import _glossary_resolver, app
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import MetricLossError, PolicyLossError
from cosmos_semantic.expressions import REF_RE
from cosmos_semantic.interchange import dump_project, import_model
from cosmos_semantic.interchange.cube import export_cube
from cosmos_semantic.interchange.ossie import VENDOR, export_ossie
from cosmos_semantic.ontology import run_rule_tests
from cosmos_semantic.planner import Planner, Principal, QueryRequest
from cosmos_semantic.reference import load_cases, run_case
from cosmos_semantic.spec import Project

from .conftest import CASES, EXAMPLE

FIXTURES = Path(__file__).parent / "fixtures"
SUL = Principal(user={"region": "Midwest"})


def rows(graph, connector, request: QueryRequest, principal=None):
    sql = (
        Planner(graph, connector.dialect, connector.config.schema_map).plan(request, principal).sql
    )
    out = connector.execute(sql).to_pylist()
    return sorted(
        (
            tuple(round(float(v), 4) if isinstance(v, int | float) else v for v in r.values())
            for r in out
        ),
        key=str,
    )


def same(a, b):
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        for u, v in zip(x, y, strict=True):
            if isinstance(u, float):
                assert math.isclose(u, v, rel_tol=1e-6), (x, y)
            else:
                assert u == v, (x, y)


def imported(kind: str, path: Path, **options):
    result = import_model(kind, path, **options)
    # The generated YAML must be a valid v1 model on its own.
    text = dump_project(result.project)
    project = Project.model_validate(yaml.safe_load(text))
    return compile_project(project), result.warnings


SOURCES = [
    "duckdb_connector",
    "postgres_connector",
    "sqlserver_connector",
    "databricks_connector",
    "snowflake_connector",
    "mysql_connector",
    "mariadb_connector",
    "oracle_connector",
]

QUERIES = [
    (QueryRequest(metrics=["revenue", "order_count"], dimensions=["orders.status"]), None),
    (QueryRequest(metrics=["completed_revenue"], dimensions=["orders.ordered_at__month"]), None),
    (QueryRequest(metrics=["average_order_value"], dimensions=["orders.status"]), None),
]


@pytest.mark.parametrize(
    "kind,path,options",
    [
        ("cube", FIXTURES / "cube", {}),
        ("dbt", FIXTURES / "dbt_legacy", {"schema": "public"}),
        ("dbt", FIXTURES / "dbt_latest", {"schema": "public"}),
    ],
    ids=["cube", "dbt-legacy", "dbt-latest"],
)
@pytest.mark.parametrize("source", SOURCES)
def test_imported_model_matches_native(kind, path, options, source, graph, request):
    connector = request.getfixturevalue(source)
    other, _ = imported(kind, path, **options)
    for query, principal in QUERIES:
        same(rows(graph, connector, query, principal), rows(other, connector, query))


def test_cube_joins_and_row_level_equivalence(graph, duckdb_connector):
    other, warnings = imported("cube", FIXTURES / "cube")
    native = rows(
        graph,
        duckdb_connector,
        QueryRequest(metrics=["revenue", "customer_count"], dimensions=["customers.segment"]),
        SUL,
    )
    cube = rows(
        other,
        duckdb_connector,
        QueryRequest(
            metrics=["revenue", "customer_count"],
            dimensions=["customers.segment"],
            filters=["{customers.region} = 'Midwest'"],
        ),
    )
    same(native, cube)
    paid = QueryRequest(metrics=["paid_value"], dimensions=["orders.status"])
    same(rows(graph, duckdb_connector, paid), rows(other, duckdb_connector, paid))
    # The segment became a business rule and works as a filter
    seg = rows(
        other,
        duckdb_connector,
        QueryRequest(metrics=["revenue"], filters=["{rule.completed_orders}"]),
    )
    done = rows(graph, duckdb_connector, QueryRequest(metrics=["completed_revenue"]))
    same(seg, done)
    joined = " ".join(warnings)
    assert "geo" in joined and "pre_aggregations" in joined and "views" in joined


def test_cube_rejects_code_and_templates(tmp_path):
    (tmp_path / "orders.js").write_text("cube(`orders`, {})", encoding="utf-8")
    (tmp_path / "t.yml").write_text(
        "cubes:\n  - name: x\n    sql_table: {{ t }}\n", encoding="utf-8"
    )
    (tmp_path / "cube.js").write_text("module.exports = {queryRewrite: () => {}}", encoding="utf-8")
    result = import_model("cube", tmp_path)
    joined = " ".join(result.warnings)
    assert "JavaScript" in joined and "template" in joined and "row_filters" in joined
    assert result.project.semantic_models == []


def test_dbt_reports_what_was_not_imported():
    _, legacy = imported("dbt", FIXTURES / "dbt_legacy", schema="public")
    assert any("percentile" in w for w in legacy)
    assert any("offset_window" in w for w in legacy)
    _, latest = imported("dbt", FIXTURES / "dbt_latest", schema="public")
    assert any("conversion" in w for w in latest)


def test_dbt_jinja_filter_becomes_reference(graph, duckdb_connector):
    other, _ = imported("dbt", FIXTURES / "dbt_legacy", schema="public")
    assert other.metrics["midwest_revenue"].filter == "{customers.region} = 'Midwest'"
    native = rows(
        graph,
        duckdb_connector,
        QueryRequest(metrics=["revenue"], dimensions=["customers.region"]),
        SUL,
    )
    dbt = rows(other, duckdb_connector, QueryRequest(metrics=["midwest_revenue"]))
    assert math.isclose(native[0][1], dbt[0][0], rel_tol=1e-6)


def test_ossie_import(graph, duckdb_connector):
    other, warnings = imported("ossie", FIXTURES / "ossie" / "ecommerce.yaml")
    assert not warnings
    for metric in ("revenue", "order_count", "average_order_value"):
        request = QueryRequest(metrics=[metric], dimensions=["orders.status"])
        same(rows(graph, duckdb_connector, request), rows(other, duckdb_connector, request))
    per_customer = rows(other, duckdb_connector, QueryRequest(metrics=["revenue_per_customer"]))
    total = rows(graph, duckdb_connector, QueryRequest(metrics=["revenue"]))[0][0]
    count = duckdb_connector.execute("SELECT COUNT(*) AS n FROM public.customers").to_pylist()[0][
        "n"
    ]
    assert math.isclose(per_customer[0][0], total / count, rel_tol=1e-6)
    assert other.project.glossary[0].alt_labels["pt"] == ["região"]


def test_ossie_restores_cosmos_projects_without_loss(tmp_path, graph):
    doc = {
        "version": "0.2.0.dev0",
        "name": "x",
        "datasets": [],
        "custom_extensions": [
            {
                "vendor_name": VENDOR,
                "data": json.dumps({"project": graph.project.model_dump(mode="json")}),
            }
        ],
    }
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    result = import_model("ossie", path)
    assert result.project.model_dump(mode="json") == graph.project.model_dump(mode="json")


# -- Ossie export (the whole model: policies, glossary, rules and business context) -------

BUSINESS = ("business_rules", "teams", "systems", "processes")
REFERENCE = load_cases(CASES) + load_cases(CASES.with_name("queries_advanced.yml"))
# Metrics kept only in the Cosmos extension, and why.
EXTENSION_ONLY = {
    "collection_gap": "it spans datasets",
    "items_per_order": "it spans datasets",
    "revenue_year_to_date": "it is cumulative",
    "revenue_month_to_date": "it is cumulative",
}
# A row filter that reads a business rule.
RULE_FILTER = {"name": "active_only", "filter": "{rule.active_customer} AND {segment} <> 'x'"}
CHANGE = dt.date(2026, 7, 1)  # active_customer went from 60 to 90 days on this date


def _without_business(project: Project) -> dict:
    """The example without business rules, business context or glossary concepts that map to
    a rule: a model other formats (Cube) carry. Row filters, sensitivity, the user context and
    the rest of the glossary are kept."""
    data = project.model_dump(mode="json")
    for key in BUSINESS:
        data[key] = Project.model_fields[key].default
    data["glossary"] = [c for c in data["glossary"] if not (c["maps_to"] or "").startswith("rule:")]
    return data


def _uses_rule(case, rules: set[str]) -> bool:
    refs = REF_RE.findall(" ".join(case.request.filters)) + case.request.dimensions
    names = [r.split("__")[0] for r in refs]
    return any(r.startswith("rule.") or r.split(".")[-1] in rules for r in names)


def _numbers_cases(project: Project) -> list:
    """Reference queries without business rules, for formats that do not carry rules (Cube),
    with the user's row filter and the sensitivity policy applied."""
    rules = {r.name for r in project.business_rules}
    return [c for c in REFERENCE if c.question is None and not _uses_rule(c, rules)]


def _without_policies(data: dict) -> dict:
    for model in data["semantic_models"]:
        model["row_filters"] = []
        for dim in model["dimensions"]:
            dim["sensitivity"] = None
    data["user_context"] = {}
    return data


def _policies(project: Project) -> dict:
    return {
        "row_filters": {
            m.name: [f.model_dump(mode="json") for f in m.row_filters]
            for m in project.semantic_models
        },
        "sensitivity": {
            f"{m.name}.{d.name}": d.sensitivity
            for m in project.semantic_models
            for d in m.dimensions
            if d.sensitivity
        },
        "user_context": {k: v.model_dump(mode="json") for k, v in project.user_context.items()},
    }


def _business(project: Project) -> dict:
    """Rules, every version of each, and the business context, field by field."""
    return {
        "business_rules": [r.model_dump(mode="json") for r in project.business_rules],
        "versions": {
            r.name: [v.model_dump(mode="json") for v in r.versions()]
            for r in project.business_rules
        },
        **{k: [x.model_dump(mode="json") for x in getattr(project, k)] for k in BUSINESS[1:]},
    }


def _with_rule_filter(project: Project) -> Project:
    data = project.model_dump(mode="json")
    customers = next(m for m in data["semantic_models"] if m["name"] == "customers")
    customers["row_filters"].append(RULE_FILTER)
    return Project.model_validate(data)


def _round_trip(project: Project, tmp_path: Path, fmt: str = "yaml"):
    exported = export_ossie(project, name="ecommerce", fmt=fmt)
    path = tmp_path / f"ecommerce.{fmt}"
    path.write_text(exported.text, encoding="utf-8")
    return exported, import_model("ossie", path)


def _cosmos_extension(doc: dict) -> dict:
    return json.loads(
        next(e for e in doc["custom_extensions"] if e["vendor_name"] == VENDOR)["data"]
    )


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_ossie_export_round_trip(graph, tmp_path, fmt):
    exported, back = _round_trip(graph.project, tmp_path, fmt)
    got = back.project.model_dump(mode="json")
    want = graph.project.model_dump(mode="json")
    # Docs Rule 4: exactly the same authoring model, member by member.
    assert got["semantic_models"] == want["semantic_models"]
    assert got["metrics"] == want["metrics"]
    assert got == want
    # The policies come back exactly: the same filters, principals and personal-data marking.
    assert _policies(back.project) == _policies(graph.project)
    assert _policies(back.project)["row_filters"]["customers"] == [
        {"name": "by_region", "filter": "{region} = $user.region"}
    ]
    assert len(_policies(back.project)["sensitivity"]) == 3
    # The rules, with owner, effective dates, tests and every earlier version, and the business
    # context come back exactly too.
    assert _business(back.project) == _business(graph.project)
    rules = {r.name: r for r in back.project.business_rules}
    assert {k: len(getattr(back.project, k)) for k in BUSINESS} == {
        "business_rules": 3,
        "teams": 4,
        "systems": 3,
        "processes": 2,
    }
    assert [str(v.effective_from) for v in rules["active_customer"].versions()] == [
        "2026-01-01",
        "2026-07-01",
    ]
    assert rules["active_customer"].owner == "sales-leadership"
    assert run_rule_tests(back.project) == run_rule_tests(graph.project)
    assert all(r.passed for r in run_rule_tests(back.project))
    # The glossary too, the concept that maps to a business rule included.
    assert [c.concept for c in back.project.glossary] == [c.concept for c in graph.project.glossary]
    # The re-imported model is a valid v1 model on its own.
    compile_project(Project.model_validate(yaml.safe_load(dump_project(back.project))))
    # Only the metrics Ossie cannot express are warned about, and nothing is omitted.
    for name, reason in EXTENSION_ONLY.items():
        assert f"metrics.{name}: {reason}" in "\n".join(exported.warnings)
    assert sum("only in the" in w for w in exported.warnings) == len(EXTENSION_ONLY)
    assert len(exported.warnings) == len(EXTENSION_ONLY)
    assert back.warnings == []
    assert "omitted" not in _cosmos_extension(yaml.safe_load(exported.text))


def test_ossie_import_reports_omissions_of_older_exports(graph, tmp_path):
    """A document from an earlier slice records what it omitted; the import still says so."""
    doc = yaml.safe_load(export_ossie(graph.project).text)
    root = next(e for e in doc["custom_extensions"] if e["vendor_name"] == VENDOR)
    data = json.loads(root["data"])
    data["omitted"] = {"row_filters": ["customers.active_only"], "teams": 4}
    root["data"] = json.dumps(data)
    assert _import_doc(doc, tmp_path).warnings == [
        "row_filters: the export omitted customers.active_only; this model does not have that "
        "row-level security",
        "teams: the export omitted 4 item(s)",
    ]


def test_ossie_export_policies_in_the_document(graph):
    """Row filters and sensitivity sit next to their dataset and field too, and the glossary
    becomes Ossie's own ai_context of the member each concept maps to."""
    doc = yaml.safe_load(export_ossie(graph.project).text)
    datasets = {d["name"]: d for d in doc["datasets"]}
    customers = datasets["customers"]
    assert json.loads(customers["custom_extensions"][0]["data"]) == {
        "timezone": "America/New_York",
        "row_filters": [{"name": "by_region", "filter": "{region} = $user.region"}],
        "user_context": {
            "region": {
                "claim": "region",
                "required": True,
                "description": "Region the user is allowed to see",
            }
        },
    }
    fields = {f["name"]: f for f in customers["fields"]}
    marked = {
        name: json.loads(f["custom_extensions"][0]["data"])["sensitivity"]
        for name, f in fields.items()
        if "custom_extensions" in f
    }
    assert marked == {"email": "personal", "tax_id": "personal", "health_plan": "sensitive"}
    assert all("custom_extensions" not in d for n, d in datasets.items() if n != "customers")
    assert fields["region"]["ai_context"] == {
        "synonyms": ["Customer region", "Região do cliente", "região", "region"],
        "instructions": "Region where the customer lives",
    }
    assert "comprador" in fields["customer_id"]["ai_context"]["synonyms"]  # entity:customer
    metrics = {m["name"]: m for d in doc["datasets"] for m in d.get("metrics", [])}
    assert metrics["revenue"]["ai_context"]["synonyms"][:2] == ["Gross revenue", "Receita bruta"]
    assert metrics["net_revenue"]["ai_context"]["instructions"] == "Gross revenue minus discounts"
    # A concept that maps to a rule has no native member: it is only in the root extension.
    assert "Cliente ativo" not in json.dumps(doc["datasets"], ensure_ascii=False)


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_export_native_part_keeps_policies(graph, tmp_path, source, request):
    """Without the root extension, a Cosmos import still restores the row filters, the user
    context they read and the sensitivity, so the row-filter and sensitivity reference queries
    give the same results."""
    connector = request.getfixturevalue(source)
    doc = yaml.safe_load(export_ossie(graph.project).text)
    doc["custom_extensions"] = [e for e in doc["custom_extensions"] if e["vendor_name"] != VENDOR]
    path = tmp_path / "native.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    native, warnings = imported("ossie", path)
    assert _policies(native.project) == _policies(graph.project)
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "the row filters and sensitivity of customers.by_region, customers.email, "
        "customers.tax_id, customers.health_plan were read from the COSMOS_SEMANTIC copies"
    )
    assert "verify them before use" in warnings[0]
    assert "business rules no row filter reads, teams, systems and processes" in warnings[0]
    cases = [c for c in REFERENCE if c.id in ("R17", "R18")]
    assert len(cases) == 2
    for case in cases:
        result = run_case(case, native, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"


def _native_doc(project: Project) -> dict:
    """The exported document without the root Cosmos extension."""
    doc = yaml.safe_load(export_ossie(project).text)
    doc["custom_extensions"] = [e for e in doc["custom_extensions"] if e["vendor_name"] != VENDOR]
    return doc


def _import_doc(doc: dict, tmp_path: Path):
    path = tmp_path / "doc.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return import_model("ossie", path)


def test_ossie_import_warns_on_root_extension_without_project(graph, tmp_path):
    doc = _native_doc(graph.project)
    doc["custom_extensions"].append({"vendor_name": VENDOR, "data": json.dumps({"x": 1})})
    result = _import_doc(doc, tmp_path)
    assert result.warnings[0] == (
        "custom_extensions: the COSMOS_SEMANTIC extension at the root has no project; the "
        "model is read from the native part of the document"
    )
    assert "were read from the COSMOS_SEMANTIC copies" in result.warnings[1]
    assert _policies(result.project) == _policies(graph.project)


def test_ossie_import_refuses_conflicting_user_context(graph, tmp_path):
    data = graph.project.model_dump(mode="json")
    sellers = next(m for m in data["semantic_models"] if m["name"] == "sellers")
    sellers["row_filters"] = [{"name": "by_region", "filter": "{region} = $user.region"}]
    doc = _native_doc(Project.model_validate(data))
    # The same declaration in two datasets is fine.
    assert _policies(_import_doc(doc, tmp_path).project)["user_context"] == {
        "region": graph.project.user_context["region"].model_dump(mode="json")
    }
    ds = next(d for d in doc["datasets"] if d["name"] == "sellers")
    copy = json.loads(ds["custom_extensions"][0]["data"])
    copy["user_context"]["region"]["claim"] = "home_region"
    ds["custom_extensions"][0]["data"] = json.dumps(copy)
    with pytest.raises(ValueError, match="user_context.region is declared in conflicting ways"):
        _import_doc(doc, tmp_path)
    copy["user_context"]["region"]["claim"] = "region"
    copy["user_context"]["region"]["required"] = False
    ds["custom_extensions"][0]["data"] = json.dumps(copy)
    with pytest.raises(ValueError, match="datasets.sellers: user_context.region"):
        _import_doc(doc, tmp_path)


def test_ossie_import_rejects_invalid_extension_json(graph, tmp_path):
    doc = _native_doc(graph.project)
    customers = next(d for d in doc["datasets"] if d["name"] == "customers")
    email = next(f for f in customers["fields"] if f["name"] == "email")
    email["custom_extensions"][0]["data"] = "{not json"
    with pytest.raises(
        ValueError,
        match=r"datasets.customers.fields.email.custom_extensions: the COSMOS_SEMANTIC "
        r"extension is not valid JSON",
    ):
        _import_doc(doc, tmp_path)
    doc = _native_doc(graph.project)
    doc["custom_extensions"].append({"vendor_name": VENDOR, "data": "[1"})
    with pytest.raises(ValueError, match=r"^custom_extensions: the COSMOS_SEMANTIC extension"):
        _import_doc(doc, tmp_path)


def test_ossie_import_fallback_reports_user_metrics(graph, tmp_path):
    """A metric whose filter reads $user has no native expression; without the root extension
    it is missing, and the import says so instead of dropping it, or its user context,
    silently."""
    data = graph.project.model_dump(mode="json")
    data["metrics"].append(
        {
            "name": "my_status_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{status} = $user.region",
        }
    )
    project = Project.model_validate(data)
    doc = yaml.safe_load(export_ossie(project).text)
    root = _cosmos_extension(doc)["project"]
    assert "my_status_revenue" in {m["name"] for m in root["metrics"]}
    assert list(root["user_context"]) == ["region"]
    natives = {m["name"] for d in doc["datasets"] for m in d.get("metrics", [])}
    assert "my_status_revenue" not in natives
    result = _import_doc(_native_doc(project), tmp_path)
    assert "my_status_revenue" not in {m.name for m in result.project.metrics}
    assert "metrics whose filter reads $user" in result.warnings[-1]
    assert "are missing" in result.warnings[-1]


def test_ossie_export_row_filter_reading_a_rule(graph, tmp_path):
    """A row filter that reads a business rule is exported with the rule, without any flag, and
    comes back exactly; the dataset's copy carries the rule too, for the fallback import."""
    project = _with_rule_filter(graph.project)
    exported, back = _round_trip(project, tmp_path)
    assert back.project.model_dump(mode="json") == project.model_dump(mode="json")
    assert _policies(back.project) == _policies(project)
    assert _business(back.project) == _business(project)
    assert len(exported.warnings) == len(EXTENSION_ONLY)
    assert back.warnings == []
    doc = yaml.safe_load(exported.text)
    customers = next(d for d in doc["datasets"] if d["name"] == "customers")
    copy = json.loads(customers["custom_extensions"][0]["data"])
    assert copy["row_filters"][-1] == RULE_FILTER
    assert copy["business_rules"] == [
        project.business_rules[0].model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    ]
    # Without the root extension, the row filter and the rule it reads are restored from the
    # dataset's copy, every version included.
    fallback = _import_doc(_native_doc(project), tmp_path)
    assert _policies(fallback.project) == _policies(project)
    (rule,) = fallback.project.business_rules
    assert rule.model_dump(mode="json") == project.business_rules[0].model_dump(mode="json")
    assert "with the business rules they read (active_customer)" in fallback.warnings[0]
    # A rule copied with two definitions is refused.
    doc = _native_doc(project)
    ds = next(d for d in doc["datasets"] if d["name"] == "customers")
    other = {**copy["business_rules"][0], "owner": "someone-else"}
    copy["business_rules"].append(other)
    ds["custom_extensions"] = [{"vendor_name": VENDOR, "data": json.dumps(copy)}]
    with pytest.raises(ValueError, match="business rule `active_customer` is defined in"):
        _import_doc(doc, tmp_path)


def _active_by_segment(graph, connector, **dates):
    request = QueryRequest(metrics=["customer_count"], dimensions=["customers.segment"], **dates)
    return rows(graph, connector, request, SUL)


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_export_rule_filter_and_versions_same_numbers(graph, tmp_path, source, request):
    """The round-tripped row filter that reads a rule filters the same rows, today and when a
    query is replayed as of a past date, under the rule version in effect then; the rule's
    versions give the same numbers as the original's, as a filter and as a grouping."""
    connector = request.getfixturevalue(source)
    original = compile_project(_with_rule_filter(graph.project))
    _, back = _round_trip(original.project, tmp_path)
    restored = compile_project(back.project)
    fallback = compile_project(_import_doc(_native_doc(original.project), tmp_path).project)
    before = CHANGE - dt.timedelta(days=10)
    after = CHANGE + dt.timedelta(days=10)
    today = dt.date.today()
    dates = [
        {},
        {"as_of": before},
        {"as_of": before, "rules_as_of": CHANGE},
        {"as_of": after, "rules_as_of": before},  # after the change, under the old version
    ]
    for when in dates:
        want = _active_by_segment(original, connector, **when)
        same(want, _active_by_segment(restored, connector, **when))
        same(want, _active_by_segment(fallback, connector, **when))
    # The row filter does filter: fewer customers than with the region filter alone.
    alone = sum(r[-1] for r in _active_by_segment(graph, connector, as_of=today))
    assert sum(r[-1] for r in _active_by_segment(original, connector, as_of=today)) < alone
    # The rules' versions, without the extra row filter, as of a past date and today.
    _, plain = _round_trip(graph.project, tmp_path)
    other = compile_project(plain.project)
    queries = [
        QueryRequest(metrics=["customer_count"], filters=["{rule.active_customer}"]),
        QueryRequest(metrics=["revenue"], dimensions=["orders.order_size_band"]),
        QueryRequest(metrics=["order_count"], filters=["{order_net_value} > 1000"]),
    ]
    for query in queries:
        for when in dates:
            replay = query.model_copy(update=when)
            same(rows(graph, connector, replay, SUL), rows(other, connector, replay, SUL))


def _with_chained_rule(project: Project) -> Project:
    """A rule whose input is bound to another rule, read by a row filter."""
    data = project.model_dump(mode="json")
    data["business_rules"].append(
        {
            "name": "loyal_active",
            "kind": "predicate",
            "owner": "sales-leadership",
            "effective_from": "2026-01-01",
            "inputs": {"active": "boolean", "segment": "categorical"},
            "logic": "{active} AND {segment} <> 'x'",
            "applies_to": [
                {"model": "customers", "bind": {"active": "active_customer", "segment": "segment"}}
            ],
        }
    )
    customers = next(m for m in data["semantic_models"] if m["name"] == "customers")
    customers["row_filters"].append({"name": "loyal", "filter": "{rule.loyal_active}"})
    return Project.model_validate(data)


def _native_copy(project: Project, dataset: str) -> dict:
    ds = next(d for d in _native_doc(project)["datasets"] if d["name"] == dataset)
    return json.loads(ds["custom_extensions"][0]["data"])


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_fallback_keeps_chained_rules(graph, tmp_path, source, request):
    """A row filter reads a rule bound to another rule: the dataset copy carries both, in the
    order of `business_rules`, so the fallback import filters the same rows."""
    connector = request.getfixturevalue(source)
    project = _with_chained_rule(graph.project)
    original = compile_project(project)
    copy = _native_copy(project, "customers")
    assert [r["name"] for r in copy["business_rules"]] == ["active_customer", "loyal_active"]
    result = _import_doc(_native_doc(project), tmp_path)
    rules = project.business_rules
    assert result.project.business_rules == [rules[0], rules[-1]]
    fallback = compile_project(result.project)
    for when in ({}, {"as_of": CHANGE - dt.timedelta(days=10)}):
        same(
            _active_by_segment(original, connector, **when),
            _active_by_segment(fallback, connector, **when),
        )


class _FrozenClock(dt.datetime):
    """2026-07-01 02:30 UTC: still 2026-06-30, under the 60-day version, in New York."""

    NOW = dt.datetime(2026, 7, 1, 2, 30, tzinfo=dt.UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.NOW.astimezone(tz) if tz else cls.NOW.replace(tzinfo=None)


def test_ossie_fallback_keeps_the_time_zone(graph, tmp_path, duckdb_connector, monkeypatch):
    """Without `as_of`, "today" is the business time zone's: the fallback import restores it
    from the dataset copies, so an evening query in New York (the next day in UTC) uses the
    same rule version and filters the same rows as the original."""
    from cosmos_semantic import planner

    project = _with_rule_filter(graph.project)
    assert project.timezone == "America/New_York"
    assert _native_copy(project, "customers")["timezone"] == "America/New_York"
    fallback = _import_doc(_native_doc(project), tmp_path).project
    assert fallback.timezone == project.timezone
    clock = types.SimpleNamespace(**{**vars(dt), "datetime": _FrozenClock})
    monkeypatch.setattr(planner, "dt", clock)
    request = QueryRequest(metrics=["customer_count"], dimensions=["customers.segment"])
    plans = {
        label: Planner(compile_project(p), "duckdb").plan(request, SUL)
        for label, p in (
            ("original", project),
            ("fallback", fallback),
            ("utc", project.model_copy(update={"timezone": None})),
        )
    }
    assert plans["original"].rules_as_of == dt.date(2026, 6, 30)
    assert plans["fallback"].rules_as_of == dt.date(2026, 6, 30)
    assert plans["utc"].rules_as_of == dt.date(2026, 7, 1)  # what a lost time zone gives
    assert plans["fallback"].sql == plans["original"].sql

    def result(label):
        return sorted(map(str, duckdb_connector.execute(plans[label].sql).to_pylist()))

    assert result("fallback") == result("original")
    # Copies that disagree on the time zone are refused.
    data = project.model_dump(mode="json")
    sellers = next(m for m in data["semantic_models"] if m["name"] == "sellers")
    sellers["row_filters"] = [{"name": "by_region", "filter": "{region} = $user.region"}]
    doc = _native_doc(Project.model_validate(data))
    ds = next(d for d in doc["datasets"] if d["name"] == "sellers")
    copy = json.loads(ds["custom_extensions"][0]["data"])
    copy["timezone"] = "UTC"
    ds["custom_extensions"][0]["data"] = json.dumps(copy)
    with pytest.raises(ValueError, match="datasets.sellers: the COSMOS_SEMANTIC copies declare"):
        _import_doc(doc, tmp_path)


def test_ossie_export_reports_unused_user_context(graph):
    data = graph.project.model_dump(mode="json")
    data["user_context"]["team"] = {"claim": "team"}
    exported = export_ossie(Project.model_validate(data))
    assert (
        "user_context: team not exported to Ossie yet (no exported row filter or metric uses them)"
        in (exported.warnings)
    )
    doc = yaml.safe_load(exported.text)
    assert _cosmos_extension(doc)["omitted"] == {"user_context": 1}
    assert list(_cosmos_extension(doc)["project"]["user_context"]) == ["region"]


def _with_test_metrics(project: Project) -> Project:
    """The example plus a filtered count, a filtered count_distinct and a filtered avg."""
    data = _without_policies(_without_business(project))
    data["glossary"] = []
    payments = next(m for m in data["semantic_models"] if m["name"] == "payments")
    payments["measures"].append({"name": "avg_paid", "agg": "avg", "expr": "value"})
    data["metrics"] += [
        {"name": "completed_orders", "type": "simple", "measure": "order_count"},
        {"name": "completed_buyers", "type": "simple", "measure": "buyers"},
        {"name": "avg_card_payment", "type": "simple", "measure": "avg_paid"},
    ]
    data["metrics"][-3]["filter"] = "{status} = 'completed'"
    data["metrics"][-2]["filter"] = "{orders.status} = 'completed'"
    data["metrics"][-1]["filter"] = "{method} = 'card'"
    return Project.model_validate(data)


def test_ossie_export_native_document(graph):
    exported = export_ossie(_with_test_metrics(graph.project), name="ecommerce")
    doc = yaml.safe_load(exported.text)
    assert doc["version"] == "0.2.0.dev0" and doc["name"] == "ecommerce"
    datasets = {d["name"]: d for d in doc["datasets"]}
    assert set(datasets) == {m.name for m in graph.project.semantic_models}
    assert datasets["orders"]["source"] == "public.orders"
    assert datasets["orders"]["primary_key"] == ["order_id"]
    fields = {f["name"]: f for f in datasets["orders"]["fields"]}
    assert fields["ordered_at"]["dimension"] == {"is_time": True}
    assert fields["has_discount"]["expression"]["dialects"] == [
        {"dialect": "OSSIE_SQL_2026", "expression": "discount > 0"}
    ]
    assert "dimension" not in fields["customer_id"]
    relationships = {(r["from"], r["to"]): r for r in doc["relationships"]}
    assert relationships[("payments", "orders")]["from_columns"] == ["order_id"]
    assert len(relationships) == sum(len(e) for e in graph.edges.values())

    native: dict[str, str] = {}
    for dataset in doc["datasets"]:
        for metric in dataset.get("metrics", []):
            (dialect,) = metric["expression"]["dialects"]
            assert dialect["dialect"] == "OSSIE_SQL_2026"
            native[metric["name"]] = dialect["expression"]
    # Written as the planner computes them.
    assert native["avg_quantity"] == "AVG(CAST(quantity AS DOUBLE))"
    assert native["avg_card_payment"] == (
        "AVG(CAST(CASE WHEN method = 'card' THEN value END AS DOUBLE))"
    )
    assert native["completed_revenue"] == "SUM(CASE WHEN status = 'completed' THEN amount END)"
    assert native["completed_orders"] == "COUNT(CASE WHEN status = 'completed' THEN 1 END)"
    assert native["completed_buyers"] == (
        "COUNT(DISTINCT CASE WHEN status = 'completed' THEN customer_id END)"
    )
    assert native["average_order_value"] == "CAST(SUM(amount) AS DOUBLE) / NULLIF(COUNT(*), 0)"
    assert native["net_revenue"] == "(SUM(amount)) - (SUM(discount))"
    assert native["orders_per_buyer"] == (
        "CAST((COUNT(*)) AS DOUBLE) / NULLIF((COUNT(DISTINCT customer_id)), 0)"
    )
    # Metrics spanning datasets or cumulative: absent from the native part, kept in the
    # extension.
    assert doc["metrics"] == []
    assert not set(EXTENSION_ONLY) & set(native)
    kept = {m["name"] for m in _cosmos_extension(doc)["project"]["metrics"]}
    assert set(EXTENSION_ONLY) <= kept


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_export_same_numbers(graph, tmp_path, source, request):
    """The exported and re-imported model passes every reference case, the business rule and
    rule-version ones and the glossary questions included, as the native model does
    (tests/test_reference.py), on every database available."""
    connector = request.getfixturevalue(source)
    _, back = _round_trip(graph.project, tmp_path)
    other = compile_project(back.project)
    resolver = _glossary_resolver(other)
    assert {"R17", "R18", "A22", "A24"} <= {c.id for c in REFERENCE}
    assert len(REFERENCE) == 48
    for case in REFERENCE:
        result = run_case(case, other, connector, resolver=resolver)
        assert result.status == "pass", f"{case.id}: {result.detail}"


@pytest.mark.parametrize("source", SOURCES)
def test_ossie_export_native_part_same_numbers(graph, tmp_path, source, request):
    """A Cosmos import of the native part alone, without the Cosmos extension, returns the
    same numbers for every metric the document expresses."""
    connector = request.getfixturevalue(source)
    project = _with_test_metrics(graph.project)
    full = compile_project(project)
    doc = yaml.safe_load(export_ossie(project).text)
    doc["custom_extensions"] = [e for e in doc["custom_extensions"] if e["vendor_name"] != VENDOR]
    path = tmp_path / "native.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    native, warnings = imported("ossie", path)
    assert warnings == []
    names = [m["name"] for d in doc["datasets"] for m in d.get("metrics", [])]
    assert len(names) == 21
    for name in names:
        for dims in ([], ["orders.status"]):
            query = QueryRequest(metrics=[name], dimensions=dims)
            if dims and full.measure_owner.get(name) in ("customers", "products", "sellers"):
                continue  # those models do not reach orders
            same(rows(full, connector, query), rows(native, connector, query))


def test_ossie_export_states_each_reason(graph):
    data = _without_policies(graph.project.model_dump(mode="json"))
    data["metrics"] += [
        {
            "name": "midwest_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{customers.region} = 'Midwest'",
        },
        {
            "name": "recent_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{orders.ordered_at__month} >= DATE '2026-01-01'",
        },
        {"name": "my_revenue", "type": "simple", "measure": "revenue", "filter": "$user.id = 1"},
        {"name": "raw_gap", "type": "derived", "expr": "{revenue} - amount"},
        {
            "name": "large_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{rule.order_size_band} = 'Large'",
        },
        {
            "name": "gap_share",
            "type": "ratio",
            "numerator": "collection_gap",
            "denominator": "revenue",
        },
    ]
    warnings = export_ossie(Project.model_validate(data)).warnings
    reasons = {w.split(": ", 1)[0]: w.split(": ", 1)[1] for w in warnings}
    assert reasons["metrics.midwest_revenue"].startswith(
        "its filter uses `customers.region` from another dataset (customers)"
    )
    assert reasons["metrics.recent_revenue"].startswith(
        "its filter uses a time grain (`orders.ordered_at__month`)"
    )
    assert reasons["metrics.my_revenue"].startswith("its filter uses $user")
    assert reasons["metrics.raw_gap"].startswith("its expression uses bare columns (amount)")
    assert reasons["metrics.gap_share"].startswith("it spans datasets")
    with pytest.raises(ValueError):
        export_ossie(graph.project, fmt="xml")
    assert reasons["metrics.large_revenue"].startswith(
        "its filter uses the business rule `rule.order_size_band`, which only Cosmos evaluates"
    )


def test_cli_export_ossie(tmp_path, graph):
    runner = CliRunner()
    # The example, with a row filter, personal data and business rules, exports without flags.
    result = runner.invoke(app, ["export", "ossie", "-m", str(EXAMPLE)])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(result.stdout)
    assert doc["name"] == "ecommerce" and doc["datasets"]
    assert "warning:" not in result.stdout
    assert "business_rules" not in result.stderr and "row_filters" not in result.stderr
    out = tmp_path / "out" / "model.json"
    result = runner.invoke(
        app, ["export", "ossie", "-m", str(EXAMPLE), "-o", str(out), "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    assert "4 warnings" in result.stdout
    assert json.loads(out.read_text(encoding="utf-8"))["name"] == "ecommerce"
    back = import_model("ossie", out).project
    assert _policies(back) == _policies(graph.project)
    assert _business(back) == _business(graph.project)
    # A row filter that reads a business rule is exported too; the flag is no longer needed.
    model = tmp_path / "semantic" / "model.yml"
    model.parent.mkdir()
    model.write_text(dump_project(_with_rule_filter(graph.project)), encoding="utf-8")
    result = runner.invoke(app, ["export", "ossie", "-m", str(model)])
    assert result.exit_code == 0, result.output
    assert "active_only" not in result.stderr
    assert RULE_FILTER["filter"] in json.dumps(yaml.safe_load(result.stdout))
    # --allow-policy-loss is still accepted, for compatibility, and changes nothing.
    flagged = runner.invoke(app, ["export", "ossie", "-m", str(model), "--allow-policy-loss"])
    assert flagged.exit_code == 0, flagged.output
    assert flagged.stdout == result.stdout
    assert flagged.stderr == result.stderr
    project = _with_rule_filter(graph.project)
    assert export_ossie(project, allow_policy_loss=True) == export_ossie(project)


# -- Cube export (cubes, joins, dimensions, measures, metrics) --------------------------------

CUMULATIVE = ("revenue_year_to_date", "revenue_month_to_date")
SPANS_CUBES = ("collection_gap", "items_per_order")
LOSS_FLAGS = {"allow_policy_loss": True, "allow_metric_loss": True}


def _cube_round_trip(project: Project, tmp_path: Path, **options):
    exported = export_cube(project, **options)
    path = tmp_path / "cube" / "model.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(exported.text, encoding="utf-8")
    return exported, import_model("cube", path)


def _with_policies(back: Project, original: Project) -> Project:
    """The re-imported model with the original row filters, user context and sensitivity put
    back, to show that the export keeps everything the policies act on."""
    data = back.model_dump(mode="json")
    source = {m.name: m for m in original.semantic_models}
    for model in data["semantic_models"]:
        spec = source[model["name"]]
        model["row_filters"] = [f.model_dump(mode="json") for f in spec.row_filters]
        marked = {d.name: d.sensitivity for d in spec.dimensions}
        for dim in model["dimensions"]:
            dim["sensitivity"] = marked[dim["name"]]
    data["user_context"] = {k: v.model_dump(mode="json") for k, v in original.user_context.items()}
    return Project.model_validate(data)


def _cube_cases(project: Project, *, policies: bool) -> list:
    """Reference queries the Cube export must answer: none with a business rule or a cumulative
    metric (not exported), and, without the policies, none that reads the user or expects a
    policy error."""
    cases = [
        c
        for c in _numbers_cases(project)
        if not set(c.request.metrics) & set(CUMULATIVE)
        and (policies or not (c.user or c.expect_error))
    ]
    return cases


def test_cube_export_fails_closed(graph, tmp_path):
    """The example has row filters, sensitivity and cumulative metrics: each loss is refused
    until the caller accepts it."""
    with pytest.raises(PolicyLossError) as exc:
        export_cube(graph.project, allow_metric_loss=True)
    assert str(exc.value) == (
        "1 row filter (customers.by_region) and the sensitivity of 3 dimensions "
        "(customers.email, customers.tax_id, customers.health_plan) would NOT be exported: the "
        "Cube export does not carry access or personal-data policies yet; the Cube model would "
        "not have this row-level security or personal-data marking. Pass --allow-policy-loss to "
        "export anyway"
    )
    with pytest.raises(MetricLossError) as exc:
        export_cube(graph.project, allow_policy_loss=True)
    assert str(exc.value).startswith(
        "2 metrics (revenue_year_to_date, revenue_month_to_date) would NOT be exported "
        "(revenue_year_to_date: it is cumulative"
    )
    assert str(exc.value).endswith("Pass --allow-metric-loss to export anyway")
    # Without policies or cumulative metrics there is nothing to refuse.
    data = _without_policies(_without_business(graph.project))
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in CUMULATIVE]
    data["user_context"], data["timezone"], data["glossary"] = {}, None, []
    clean = export_cube(Project.model_validate(data))
    assert {w.split(":")[0] for w in clean.warnings} == {f"metrics.{m}" for m in SPANS_CUBES}


def test_cube_export_round_trip(graph, tmp_path):
    exported, back = _cube_round_trip(graph.project, tmp_path, **LOSS_FLAGS)
    # The importer reads the whole export, with nothing to report.
    assert back.warnings == []
    doc = yaml.safe_load(exported.text)
    cubes = {c["name"]: c for c in doc["cubes"]}
    assert set(cubes) == {m.name for m in graph.project.semantic_models}
    orders = cubes["orders"]
    assert orders["sql_table"] == "public.orders"
    assert orders["joins"] == [
        {
            "name": "customers",
            "relationship": "many_to_one",
            "sql": "{CUBE}.customer_id = {customers}.customer_id",
        }
    ]
    assert orders["dimensions"][0] == {
        "name": "order_id",
        "sql": "{CUBE}.order_id",
        "type": "string",
        "primary_key": True,
        "public": False,
    }
    measures = {m["name"]: m for c in doc["cubes"] for m in c.get("measures", [])}
    assert measures["completed_revenue"]["filters"] == [{"sql": "{CUBE}.status = 'completed'"}]
    assert measures["average_order_value"]["sql"] == "1e0 * {revenue} / NULLIF({order_count}, 0)"
    assert measures["orders_per_buyer"]["sql"] == "(1e0 * {order_count} / NULLIF({buyers}, 0))"
    assert measures["collection_gap"]["sql"] == "{net_revenue} - {payments.paid_value}"
    assert measures["avg_quantity"]["sql"] == "1e0 * {CUBE}.quantity"
    assert measures["item_revenue"]["sql"] == "{CUBE}.quantity * {CUBE}.unit_price"

    # Models, tables, dimensions and measures come back as they were (the primary key as an
    # entity named after the cube; AVG with a `1e0 *` factor, so it is real in Cube too).
    def shape(project: Project) -> dict:
        return {
            m.name: (
                m.table,
                [(d.name, d.type, d.expr, d.description) for d in m.dimensions],
                [(x.name, x.agg, x.expr) for x in m.measures if not x.name.endswith("__base")],
            )
            for m in project.semantic_models
        }

    want = shape(Project.model_validate(_without_policies(graph.project.model_dump(mode="json"))))
    want["order_items"][2][2] = ("avg_quantity", "avg", "1e0 * quantity")
    assert shape(back.project) == want
    names = {m.name for m in back.project.metrics} | {
        x.name for m in back.project.semantic_models for x in m.measures
    }
    assert {m.name for m in graph.project.metrics} - names == set(CUMULATIVE)
    # Every loss is a warning: policies, cumulative metrics and what a later slice exports;
    # metrics spanning cubes are exported, with Rule 3's warning.
    joined = "\n".join(exported.warnings)
    assert "semantic_models.customers.row_filters.by_region: not exported to Cube" in joined
    assert sum("its sensitivity is not exported" in w for w in exported.warnings) == 3
    for name in CUMULATIVE:
        assert f"metrics.{name}: it is cumulative" in joined
    for name in SPANS_CUBES:
        assert f"metrics.{name}: it spans cubes" in joined
    for key in ("business_rules", "glossary", "teams", "systems", "processes"):
        assert f"{key}: not exported to Cube yet" in joined
    assert "user_context: region" in joined
    assert "timezone: not exported to Cube. Model timestamps are local time, so keep" in joined
    assert "relative dates such as today then follow UTC in Cube, not America/New_York" in joined
    assert len(exported.warnings) == 15


@pytest.mark.parametrize("source", SOURCES)
def test_cube_export_same_numbers(graph, tmp_path, source, request):
    """Exported to Cube and imported back, the model answers the reference queries with the
    hand-written SQL's numbers, on every database available."""
    connector = request.getfixturevalue(source)
    _, back = _cube_round_trip(graph.project, tmp_path, **LOSS_FLAGS)
    other = compile_project(back.project)
    cases = _cube_cases(graph.project, policies=False)
    assert len(cases) == 25
    for case in cases:
        result = run_case(case, other, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"
    # With the original policies put back, the queries that read the user pass too.
    governed = compile_project(_with_policies(back.project, graph.project))
    cases = _cube_cases(graph.project, policies=True)
    assert {"R17", "R18"} <= {c.id for c in cases}
    assert len(cases) == 40
    for case in cases:
        result = run_case(case, governed, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"


def _cube_test_metrics(project: Project) -> Project:
    """The example without policies or cumulative metrics, plus `max` and `min` measures,
    filtered metrics of every aggregation, a filter on a dimension with an expression, and
    nested division."""
    data = _with_test_metrics(project).model_dump(mode="json")
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in CUMULATIVE]
    orders = next(m for m in data["semantic_models"] if m["name"] == "orders")
    orders["measures"].append({"name": "largest_order", "agg": "max", "expr": "amount"})
    payments = next(m for m in data["semantic_models"] if m["name"] == "payments")
    payments["measures"].append({"name": "smallest_payment", "agg": "min", "expr": "value"})
    data["metrics"] += [
        {
            "name": "discounted_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{has_discount} AND {status} <> 'canceled'",
        },
        {"name": "gross_revenue", "type": "simple", "measure": "revenue"},
        {
            "name": "largest_completed_order",
            "type": "simple",
            "measure": "largest_order",
            "filter": "{status} = 'completed'",
        },
        {"name": "share_of_net", "type": "derived", "expr": "{net_revenue} / ({revenue} / 2)"},
        {
            "name": "completed_share",
            "type": "ratio",
            "numerator": "completed_revenue",
            "denominator": "revenue",
        },
    ]
    return Project.model_validate(data)


@pytest.mark.parametrize("source", SOURCES)
def test_cube_export_metric_shapes_same_numbers(graph, tmp_path, source, request):
    """Every measure and metric the export writes returns the native numbers after the round
    trip, alone and by order status."""
    connector = request.getfixturevalue(source)
    project = _cube_test_metrics(graph.project)
    full = compile_project(project)
    exported, back = _cube_round_trip(project, tmp_path)
    assert back.warnings == []
    measures = {
        m["name"]: m for c in yaml.safe_load(exported.text)["cubes"] for m in c.get("measures", [])
    }
    assert measures["discounted_revenue"]["filters"] == [
        {"sql": "({CUBE}.discount > 0) AND {CUBE}.status <> 'canceled'"}
    ]
    assert measures["share_of_net"]["sql"] == (
        "(1e0 * {net_revenue} / NULLIF(((1e0 * {revenue} / NULLIF(2, 0))), 0))"
    )
    other = compile_project(back.project)
    names = [*full.measure_owner, *(m.name for m in project.metrics)]
    assert len(names) == 30
    assert measures["largest_order"] == {
        "name": "largest_order",
        "type": "max",
        "sql": "{CUBE}.amount",
    }
    assert measures["smallest_payment"]["type"] == "min"
    for name in names:
        for dims in ([], ["orders.status"]):
            query = QueryRequest(metrics=[name], dimensions=dims)
            if dims and full.measure_owner.get(name) in ("customers", "products", "sellers"):
                continue  # those models do not reach orders
            same(rows(full, connector, query), rows(other, connector, query))


def test_cube_export_states_each_reason(graph):
    data = _without_policies(_without_business(graph.project))
    data["metrics"] += [
        {
            "name": "midwest_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{customers.region} = 'Midwest'",
        },
        {
            "name": "recent_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{orders.ordered_at__month} >= DATE '2026-01-01'",
        },
        {"name": "my_revenue", "type": "simple", "measure": "revenue", "filter": "$user.id = 1"},
        {"name": "raw_gap", "type": "derived", "expr": "{revenue} - amount"},
        {
            "name": "completed_rate",
            "type": "ratio",
            "numerator": "revenue",
            "denominator": "order_count",
            "filter": "{status} = 'completed'",
        },
        {"name": "ytd_gap", "type": "derived", "expr": "{revenue_year_to_date} - {revenue}"},
    ]
    project = Project.model_validate(data)
    with pytest.raises(MetricLossError) as exc:
        export_cube(project)
    assert str(exc.value).startswith("8 metrics (revenue_year_to_date, revenue_month_to_date, ")
    warnings = export_cube(project, allow_metric_loss=True).warnings
    reasons = {w.split(": ", 1)[0]: w.split(": ", 1)[1] for w in warnings}
    assert reasons["metrics.midwest_revenue"].startswith(
        "its filter uses `customers.region` from another cube (customers)"
    )
    assert reasons["metrics.recent_revenue"].startswith(
        "its filter uses a time grain (`orders.ordered_at__month`)"
    )
    assert reasons["metrics.my_revenue"].startswith("its filter uses $user")
    assert reasons["metrics.raw_gap"].startswith("its expression uses bare columns (amount)")
    assert reasons["metrics.completed_rate"].startswith("it has a filter")
    assert reasons["metrics.ytd_gap"].startswith(
        "it uses `revenue_year_to_date`, which is not exported"
    )
    assert all(
        r.endswith("it is not exported to Cube")
        for k, r in reasons.items()
        if k in ("metrics.midwest_revenue", "metrics.ytd_gap")
    )


def test_cube_export_keeps_expressions_as_written():
    """Only bare columns get ``{CUBE}.``; the importer removes it and gets the same text."""
    from cosmos_semantic.interchange.cube import _cube_sql, _strip_cube

    for text, want in [
        ("amount", "{CUBE}.amount"),
        ("quantity * unit_price", "{CUBE}.quantity * {CUBE}.unit_price"),
        (
            'CASE WHEN "Net value" > 0 THEN value ELSE 0 END',
            'CASE WHEN {CUBE}."Net value" > 0 THEN {CUBE}.value ELSE 0 END',
        ),
        ("EXTRACT(year FROM ordered_at)", "EXTRACT(year FROM {CUBE}.ordered_at)"),
        ("COALESCE(o.amount, 'amount')", "COALESCE(o.amount, 'amount')"),
    ]:
        assert _cube_sql(text) == want
        assert _strip_cube(_cube_sql(text)) == text


def test_cli_export_cube(tmp_path):
    runner = CliRunner()
    refused = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE)])
    assert refused.exit_code == 1
    assert refused.stdout == ""
    assert "1 row filter (customers.by_region) and the sensitivity" in refused.stderr
    refused = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE), "--allow-policy-loss"])
    assert refused.exit_code == 1
    assert "2 metrics (revenue_year_to_date, revenue_month_to_date)" in refused.stderr
    flags = ["--allow-policy-loss", "--allow-metric-loss"]
    result = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE), *flags])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout)["cubes"]
    assert "warning:" not in result.stdout
    assert "warning: metrics.collection_gap: it spans cubes" in result.stderr
    out = tmp_path / "model" / "cubes.yml"
    result = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE), "-o", str(out), *flags])
    assert result.exit_code == 0, result.output
    assert "15 warnings" in result.stdout
    assert import_model("cube", out).warnings == []
