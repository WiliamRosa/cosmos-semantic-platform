"""Importers: the imported model returns the same numbers as the native model on the same data."""

from __future__ import annotations

import datetime as dt
import json
import math
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

from cosmos_semantic.cli import _glossary_resolver, app
from cosmos_semantic.compiler import compile_project
from cosmos_semantic.errors import MetricLossError, PolicyError, PolicyLossError, QueryError
from cosmos_semantic.expressions import REF_RE
from cosmos_semantic.interchange import dump_project, import_model
from cosmos_semantic.interchange.cube import export_cube
from cosmos_semantic.interchange.dbt import export_dbt
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
    "bigquery_connector",
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
    _, latest = imported("dbt", FIXTURES / "dbt_latest", schema="public")
    assert any("conversion" in w for w in latest)


def test_dbt_offset_window_with_alias(duckdb_connector):
    """`revenue - revenue_7d`, with `revenue_7d` an aliased `offset_window: 7 days` input of
    the same metric, becomes a growth over a shifted helper metric (DEC-36)."""
    other, warnings = imported("dbt", FIXTURES / "dbt_legacy", schema="public")
    assert other.metrics["revenue_growth"].expr == "{revenue} - {revenue_growth__revenue_7d}"
    helper = other.metrics["revenue_growth__revenue_7d"]
    assert (helper.expr, helper.offsets()) == ("{revenue}", {"revenue": "7 days"})
    assert any(
        w.startswith("metrics.revenue_growth__revenue_7d: `revenue` is read") for w in warnings
    )
    query = QueryRequest(
        metrics=["revenue", "revenue_growth__revenue_7d", "revenue_growth"],
        dimensions=["orders.ordered_at__day"],
    )
    out = rows(other, duckdb_connector, query)
    assert out
    for _, revenue, earlier, growth in out:
        assert growth is None if earlier is None else math.isclose(growth, revenue - earlier)


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
    "max_order_value_year_to_date": "it is cumulative",
    "revenue_rolling_7_days": "it is cumulative",
    "smallest_order_rolling_7_days": "it is cumulative",
    "customers_rolling_30_days": "it is cumulative",
    "revenue_prev_month": "it reads an input at an earlier period",
    "revenue_mom_growth": "it uses `revenue_prev_month`, and it reads an input at an earlier period",
    "revenue_yoy_growth": "it reads an input at an earlier period",
    "order_count_7_days_earlier": "it reads an input at an earlier period",
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
    # The other datasets carry only the business time zone, for the fallback import.
    assert all(
        d["custom_extensions"]
        == [{"vendor_name": VENDOR, "data": json.dumps({"timezone": "America/New_York"})}]
        for n, d in datasets.items()
        if n != "customers"
    )
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


def _at(local: dt.datetime) -> type[dt.datetime]:
    """A clock frozen at a local time."""

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return local.astimezone(tz) if tz else local.astimezone(dt.UTC).replace(tzinfo=None)

    return Clock


def test_ossie_fallback_keeps_the_time_zone_without_row_filters(
    graph, tables, tmp_path, duckdb_connector, monkeypatch
):
    """A model without row filters: every dataset copy carries the business time zone, so at
    22:30 in New York (the next day in UTC) "today" and the number that reads it are the same
    after the fallback import as on the original."""
    from cosmos_semantic import planner

    data = graph.project.model_dump(mode="json")
    for model in data["semantic_models"]:
        model["row_filters"] = []
    data["user_context"] = {}
    project = Project.model_validate(data)
    assert project.timezone == "America/New_York"
    assert all(
        _native_copy(project, m.name) == {"timezone": project.timezone}
        for m in project.semantic_models
    )
    fallback = _import_doc(_native_doc(project), tmp_path)
    assert fallback.project.timezone == project.timezone
    # The last day with orders, at 22:30 in New York.
    day = max(tables["orders"].column("ordered_at").to_pylist()).date()
    zone = ZoneInfo("America/New_York")
    clock = _at(dt.datetime.combine(day, dt.time(22, 30), tzinfo=zone))
    monkeypatch.setattr(planner, "dt", types.SimpleNamespace(**{**vars(dt), "datetime": clock}))
    request = QueryRequest(metrics=["order_count"], filters=["{orders.ordered_at} < CURRENT_DATE"])
    plans = {
        label: Planner(compile_project(p), "duckdb").plan(request)
        for label, p in (
            ("original", project),
            ("fallback", fallback.project),
            ("utc", project.model_copy(update={"timezone": None})),
        )
    }
    assert plans["original"].as_of == day
    assert plans["fallback"].as_of == day
    assert plans["utc"].as_of == day + dt.timedelta(days=1)  # what a lost time zone gives
    assert plans["fallback"].sql == plans["original"].sql

    def result(label):
        return duckdb_connector.execute(plans[label].sql).to_pylist()

    assert result("fallback") == result("original")
    assert result("utc") != result("original")  # the orders of that day move in


def test_ossie_fallback_refuses_conflicting_time_zones_without_row_filters(graph, tmp_path):
    """Two dataset copies that disagree on the time zone are refused, row filters or not."""
    data = graph.project.model_dump(mode="json")
    for model in data["semantic_models"]:
        model["row_filters"] = []
    data["user_context"] = {}
    doc = _native_doc(Project.model_validate(data))
    ds = next(d for d in doc["datasets"] if d["name"] == "sellers")
    ds["custom_extensions"][0]["data"] = json.dumps({"timezone": "UTC"})
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
    assert len(REFERENCE) == 71
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
    assert len(names) == 24
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
    assert "12 warnings" in result.stdout
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

CUMULATIVE = (
    "customers_rolling_30_days",
    "revenue_year_to_date",
    "revenue_month_to_date",
    "max_order_value_year_to_date",
    "revenue_rolling_7_days",
    "smallest_order_rolling_7_days",
)
# Derived metrics that read an input at an earlier period (DEC-18): no export carries them.
OFFSETS = (
    "revenue_prev_month",
    "revenue_mom_growth",
    "revenue_yoy_growth",
    "order_count_7_days_earlier",
)
# Running totals Cube carries as `to_date` rolling windows, rolling windows in days it carries
# as `trailing` ones (DEC-30), and the cumulative metrics it does not.
TO_DATE = ("revenue_year_to_date", "revenue_month_to_date")
TRAILING = ("customers_rolling_30_days", "revenue_rolling_7_days", "smallest_order_rolling_7_days")
CUBE_LOST = ("max_order_value_year_to_date", *OFFSETS)
SPANS_CUBES = ("collection_gap", "items_per_order")
# Rolling windows in days and running totals of `sum` and `count` measures (DEC-31) the dbt
# export carries, and the cumulative metric it does not (a running maximum); it carries the
# offsets as `offset_window` (DEC-36).
ROLLING = (
    "customers_rolling_30_days",
    "revenue_rolling_7_days",
    "smallest_order_rolling_7_days",
)
RUNNING = ("max_order_value_year_to_date",)
DBT_LOST = RUNNING
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


def _cube_cases(
    project: Project, *, policies: bool, lost: tuple[str, ...] = (*CUMULATIVE, *OFFSETS)
) -> list:
    """Reference queries an export must answer: none with a business rule or a metric it does
    not carry (``lost``: every cumulative one by default), and, without the policies, none that
    reads the user or expects a policy error."""
    cases = [
        c
        for c in _numbers_cases(project)
        if not set(c.request.metrics) & set(lost) and (policies or not (c.user or c.expect_error))
    ]
    return cases


def test_cube_export_fails_closed(graph, tmp_path):
    """The example's row filter and sensitivity are exported; a row filter that reads a
    business rule, a running maximum and offsets are refused until the caller accepts the
    loss."""
    with pytest.raises(PolicyLossError) as exc:
        export_cube(_with_rule_filter(graph.project), allow_metric_loss=True)
    assert str(exc.value) == (
        "1 row filter (customers.active_only) would NOT be exported (customers.active_only: it "
        "reads the business rule `rule.active_customer`); the Cube model would show those rows "
        "to every user. Pass --allow-policy-loss to export anyway"
    )
    with pytest.raises(MetricLossError) as exc:
        export_cube(graph.project)
    assert str(exc.value).startswith(
        "5 metrics (max_order_value_year_to_date, revenue_prev_month, revenue_mom_growth, "
        "revenue_yoy_growth, order_count_7_days_earlier) would NOT be exported "
        "(max_order_value_year_to_date: it is "
        "cumulative over a measure that aggregates with `max`, and the Cube export carries "
        "running totals of `sum` and `count` measures only; revenue_prev_month:"
    )
    assert (
        "revenue_prev_month: it reads an input at an earlier period (`offset`), and the Cube "
        "export does not carry offsets yet; revenue_mom_growth: it uses `revenue_prev_month`, "
        "which is not exported"
    ) in str(exc.value)
    # Without policies, the running maximum or the offsets there is nothing to refuse: the
    # year-to-date, month-to-date and rolling metrics are exported.
    data = _without_policies(_without_business(graph.project))
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in CUBE_LOST]
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
    # The row filter and sensitivity: one access policy for every role.
    assert cubes["customers"]["access_policy"] == [
        {
            "role": "*",
            "row_level": {
                "filters": [
                    {
                        "member": "region",
                        "operator": "equals",
                        "values": ["{ securityContext.region }"],
                    }
                ]
            },
            "member_level": {"includes": "*", "excludes": ["email", "tax_id", "health_plan"]},
        }
    ]
    assert cubes["customers"]["meta"] == {"owner": "digital-commerce", "row_filters": ["by_region"]}
    assert all("access_policy" not in c for n, c in cubes.items() if n != "customers")
    # They come back as the same row filter and sensitivity; the user field keeps its claim.
    policies = _policies(back.project)
    assert policies["row_filters"] == _policies(graph.project)["row_filters"]
    assert policies["sensitivity"] == _policies(graph.project)["sensitivity"]
    assert policies["user_context"] == {
        "region": {"claim": "region", "required": True, "description": None}
    }
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
    # A running total since the start of the period: a `to_date` rolling window.
    assert measures["revenue_year_to_date"] == {
        "name": "revenue_year_to_date",
        "type": "sum",
        "sql": "{CUBE}.amount",
        "rolling_window": {"type": "to_date", "granularity": "year"},
        "description": "Year-to-date revenue",
        "meta": {"owner": "controllership"},
    }
    assert measures["revenue_month_to_date"]["rolling_window"] == {
        "type": "to_date",
        "granularity": "month",
    }
    # A rolling window in days: a trailing window ending at the end of each bucket (DEC-30).
    assert measures["revenue_rolling_7_days"] == {
        "name": "revenue_rolling_7_days",
        "type": "sum",
        "sql": "{CUBE}.amount",
        "rolling_window": {"trailing": "7 day", "offset": "end"},
        "description": "Revenue of the 7 days ending on each day",
        "meta": {"owner": "controllership"},
    }
    assert measures["smallest_order_rolling_7_days"]["type"] == "min"
    assert measures["customers_rolling_30_days"]["rolling_window"] == {
        "trailing": "30 day",
        "offset": "end",
    }

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
    assert {m.name for m in graph.project.metrics} - names == set(CUBE_LOST)
    # The running totals come back as cumulative metrics over a `__base` measure.
    metrics = {m.name: m for m in back.project.metrics}
    for name in TO_DATE:
        native = graph.metrics[name]
        assert metrics[name].type == "cumulative"
        assert metrics[name].grain_to_date == native.grain_to_date
        assert metrics[name].window is None
        assert metrics[name].measure == f"{name}__base"
        assert metrics[name].description == native.description
    # The rolling windows come back as the same window in days.
    for name in TRAILING:
        native = graph.metrics[name]
        assert metrics[name].type == "cumulative"
        assert metrics[name].window == native.window
        assert metrics[name].grain_to_date is None
        assert metrics[name].measure == f"{name}__base"
    orders_back = next(m for m in back.project.semantic_models if m.name == "orders")
    base = next(x for x in orders_back.measures if x.name == "revenue_year_to_date__base")
    assert (base.agg, base.expr) == ("sum", "amount")
    # Every loss is a warning: the running maximum, the offsets and what a later slice
    # exports; metrics spanning cubes are exported, with Rule 3's warning.
    joined = "\n".join(exported.warnings)
    assert "row_filters" not in joined and "sensitivity" not in joined
    assert "metrics.max_order_value_year_to_date: it is cumulative over a measure" in joined
    for name in (*TO_DATE, *TRAILING):
        assert f"metrics.{name}:" not in joined
    for name in SPANS_CUBES:
        assert f"metrics.{name}: it spans cubes" in joined
    for key in ("business_rules", "glossary", "teams", "systems", "processes"):
        assert f"{key}: not exported to Cube yet" in joined
    assert (
        "user_context: the row filters read $user.region as securityContext.region. Cosmos "
        "denies a query when the user lacks one; Cube must reject such a security context too"
    ) in joined
    assert "timezone: not exported to Cube. Model timestamps are local time, so keep" in joined
    assert "relative dates such as today then follow UTC in Cube, not America/New_York" in joined
    assert len(exported.warnings) == 14


@pytest.mark.parametrize("source", SOURCES)
def test_cube_export_same_numbers(graph, tmp_path, source, request):
    """Exported to Cube and imported back, the model answers the reference queries with the
    hand-written SQL's numbers, on every database available."""
    connector = request.getfixturevalue(source)
    _, back = _cube_round_trip(graph.project, tmp_path, allow_metric_loss=True)
    ungoverned = _without_policies(back.project.model_dump(mode="json"))
    other = compile_project(Project.model_validate(ungoverned))
    cases = _cube_cases(graph.project, policies=False, lost=CUBE_LOST)
    # A08 (year-to-date by month), A09 (month-to-date by status and day) and the rolling
    # windows A29, A30 (by status, from 45 days ago) and A34 (a minimum) among them.
    assert {"A08", "A09", "A29", "A30", "A34"} <= {c.id for c in cases}
    assert len(cases) == 34
    for case in cases:
        result = run_case(case, other, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"
    # With the policies the export carried (access_policy), the queries that read the user
    # return the same rows per principal, and the refused ones are refused.
    governed = compile_project(back.project)
    cases = _cube_cases(graph.project, policies=True, lost=CUBE_LOST)
    # A32 (month-to-date by quarter) and A39 (year-to-date by week) are refused by the
    # re-imported model too; A33 is the 30-day window of customers by segment.
    assert {"R17", "R18", "A08", "A09", "A32", "A33", "A39", "A40", "A41"} <= {c.id for c in cases}
    assert len(cases) == 57
    for case in cases:
        result = run_case(case, governed, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"


# Row filters with an exact Cube access_policy equivalent: AND and OR groups, IN, $user on
# either side, and ordering on a number dimension against a number or $user.
CUBE_POLICY_FILTERS = {
    "customers": [
        {"name": "by_region", "filter": "$user.region = {region}"},
        {
            "name": "segments",
            "filter": "{segment} IN ('Consumer', 'SMB', 'Enterprise') AND "
            "({state} = 'NY' OR {customers.region} = $user.region OR {segment} = 'O''Brien')",
        },
    ],
    "payments": [
        {"name": "installments", "filter": "{installments} <= $user.max_installments"},
        {"name": "methods", "filter": "{method} IN ('card', 'wallet') OR 2 < {installments}"},
    ],
}
CUBE_PRINCIPALS = [
    {"region": "Midwest", "max_installments": 3},
    {"region": "South", "max_installments": 1},
    {"region": "West", "max_installments": 12},
    {"region": "Atlântida", "max_installments": 0},
]
CUBE_POLICY_QUERIES = [
    QueryRequest(metrics=["customer_count"], dimensions=["customers.segment", "customers.state"]),
    QueryRequest(metrics=["revenue", "order_count"], dimensions=["customers.region"]),
    QueryRequest(metrics=["paid_value", "payment_count"], dimensions=["payments.method"]),
    QueryRequest(metrics=["paid_value"], dimensions=["payments.installments"]),
    QueryRequest(metrics=["revenue"], dimensions=["orders.status"]),
]


def _with_cube_policies(project: Project, filters: dict) -> Project:
    data = project.model_dump(mode="json")
    for model in data["semantic_models"]:
        model["row_filters"] = filters.get(model["name"], [])
    data["user_context"]["max_installments"] = {"claim": "max_installments"}
    return Project.model_validate(data)


@pytest.mark.parametrize("source", SOURCES)
def test_cube_access_policy_same_rows_per_principal(graph, tmp_path, source, request):
    """Each translatable row filter shape survives the round trip: the re-imported model
    returns the same rows as the original for every principal, and denies the same queries
    when the user lacks a field a row filter reads."""
    connector = request.getfixturevalue(source)
    project = _with_cube_policies(graph.project, CUBE_POLICY_FILTERS)
    exported, back = _cube_round_trip(project, tmp_path, allow_metric_loss=True)
    assert back.warnings == []
    cubes = {c["name"]: c for c in yaml.safe_load(exported.text)["cubes"]}
    assert cubes["customers"]["access_policy"][0]["row_level"]["filters"][1] == {
        "and": [
            {
                "member": "segment",
                "operator": "equals",
                "values": ["Consumer", "SMB", "Enterprise"],
            },
            {
                "or": [
                    {"member": "state", "operator": "equals", "values": ["NY"]},
                    {
                        "member": "region",
                        "operator": "equals",
                        "values": ["{ securityContext.region }"],
                    },
                    {"member": "segment", "operator": "equals", "values": ["O'Brien"]},
                ]
            },
        ]
    }
    assert cubes["payments"]["access_policy"] == [
        {
            "role": "*",
            "row_level": {
                "filters": [
                    {
                        "member": "installments",
                        "operator": "lte",
                        "values": ["{ securityContext.max_installments }"],
                    },
                    {
                        "or": [
                            {
                                "member": "method",
                                "operator": "equals",
                                "values": ["card", "wallet"],
                            },
                            {"member": "installments", "operator": "gt", "values": ["2"]},
                        ]
                    },
                ]
            },
        }
    ]
    policies = _policies(back.project)
    assert policies["row_filters"]["payments"] == [
        {"name": "installments", "filter": "{installments} <= $user.max_installments"},
        {"name": "methods", "filter": "{method} IN ('card', 'wallet') OR {installments} > 2"},
    ]
    assert set(policies["user_context"]) == {"region", "max_installments"}
    native, other = compile_project(project), compile_project(back.project)
    seen = set()
    for query in CUBE_POLICY_QUERIES:
        for user in CUBE_PRINCIPALS:
            principal = Principal(user=user)
            want = rows(native, connector, query, principal)
            same(want, rows(other, connector, query, principal))
            seen.add(len(want))
        # A user without a field a row filter reads is denied by both, on the governed cubes.
        governed = {r.split(".")[0] for r in query.dimensions} & set(CUBE_POLICY_FILTERS)
        for model in (native, other) if governed else ():
            with pytest.raises(PolicyError):
                rows(model, connector, query, Principal(user={}))
    # The principals see different rows, so the comparison means something.
    assert len(seen) > 2


# Row filters Cube's access_policy cannot express exactly, and why.
CUBE_REFUSED_FILTERS = {
    "{rule.active_customer}": "it reads the business rule `rule.active_customer`",
    "{segment} <> 'x'": "its condition `{segment} <> 'x'` has no exact Cube filter",
    "NOT {segment} IN ('x')": "its condition `NOT {segment} IN ('x')` has no exact Cube filter",
    "{region} IS NULL": "its condition `{region} IS NULL` has no exact Cube filter",
    "LOWER({region}) = $user.region": "`LOWER({region})` is not a dimension of the cube",
    "{segment} > 'a'": "it orders the categorical dimension `segment`",
    "{created_at} >= '2026-01-01'": "it filters the time dimension `created_at`",
    "{created_at__month} = '2026-01-01'": "it uses a time grain (`created_at__month`)",
    "{email} = $user.region": "it filters `email`, which member_level hides",
    "{state} = {region}": "it compares `region` with `{state}`",
    "{segment} = 1": "it compares `segment` with `1`",
    "{segment} = '{ securityContext.x }'": "it compares `segment` with",
    "{segment} = $user.org_region": "`$user.org_region` reads the claim `org.region`",
}


@pytest.mark.parametrize("text", list(CUBE_REFUSED_FILTERS))
def test_cube_access_policy_refuses_inexact_filters(graph, tmp_path, text):
    """A row filter without an exact Cube equivalent is refused; with --allow-policy-loss it
    is a warning and only the exact ones are exported."""
    data = graph.project.model_dump(mode="json")
    data["user_context"]["org_region"] = {"claim": "org.region"}
    customers = next(m for m in data["semantic_models"] if m["name"] == "customers")
    customers["row_filters"].append({"name": "inexact", "filter": text})
    project = Project.model_validate(data)
    reason = CUBE_REFUSED_FILTERS[text]
    with pytest.raises(PolicyLossError) as exc:
        export_cube(project, allow_metric_loss=True)
    message = str(exc.value)
    assert message.startswith("1 row filter (customers.inexact) would NOT be exported")
    assert reason in message and "--allow-policy-loss" in message
    exported, back = _cube_round_trip(
        project, tmp_path, allow_metric_loss=True, allow_policy_loss=True
    )
    warning = next(w for w in exported.warnings if "row_filters.inexact" in w)
    assert warning.startswith("semantic_models.customers.row_filters.inexact: ")
    assert reason in warning
    assert warning.endswith("the Cube model does not have this row-level security")
    # The exact row filter is still exported and comes back.
    assert _policies(back.project)["row_filters"]["customers"] == [
        {"name": "by_region", "filter": "{region} = $user.region"}
    ]


_POLICY_CUBE = {
    "name": "t",
    "sql_table": "public.t",
    "dimensions": [
        {"name": "id", "sql": "{CUBE}.id", "type": "string", "primary_key": True},
        {"name": "region", "sql": "{CUBE}.region", "type": "string"},
        {"name": "secret", "sql": "{CUBE}.secret", "type": "string"},
        {"name": "level", "sql": "{CUBE}.level", "type": "number"},
    ],
    "measures": [{"name": "n", "type": "count"}],
}
_REGION = {"member": "region", "operator": "equals", "values": ["{ securityContext.region }"]}


def _policy_file(tmp_path: Path, label: str, *cubes: dict) -> Path:
    path = tmp_path / f"{label.replace(' ', '_')}.yml"
    path.write_text(yaml.safe_dump({"cubes": list(cubes)}), encoding="utf-8")
    return path


def _row_level(*filters: dict) -> list:
    return [{"role": "*", "row_level": {"filters": list(filters)}}]


# access_policy shapes Cosmos cannot express, and the reason the import gives.
CUBE_UNREADABLE_POLICIES = {
    "manager role": (
        [{"role": "manager", "row_level": {"filters": [_REGION]}}],
        'it is not one policy for every role (`role: "*"`)',
    ),
    "two policies": ([{"role": "*"}, {"role": "*"}], "it is not one policy for every role"),
    "allow_all": (
        [{"role": "*", "row_level": {"allow_all": True}}],
        "it is not one policy for every role",
    ),
    "notEquals": (
        _row_level({**_REGION, "operator": "notEquals"}),
        "its row_level filters cannot be read: the operator `notEquals`",
    ),
    "unknown member": (
        _row_level({**_REGION, "member": "x"}),
        "`x` is not a string or number dimension of the cube",
    ),
    "other cube": (
        _row_level({**_REGION, "member": "orders.region"}),
        "`orders.region`, not a member of this cube",
    ),
    "template": (_row_level({**_REGION, "values": ["{ x.y }"]}), "the template `{ x.y }`"),
    "exponent": (
        _row_level({"member": "level", "operator": "gt", "values": ["1e1"]}),
        "`1e1` is not a number",
    ),
    "exclude": (
        [{"role": "*", "member_level": {"excludes": ["secret"]}}],
        "it hides `secret` from every user",
    ),
}


@pytest.mark.parametrize("label", list(CUBE_UNREADABLE_POLICIES))
def test_cube_import_refuses_policies_it_cannot_express(tmp_path, label):
    """Importing part of an access_policy would grant more access than Cube did: refused,
    naming the cube and the reason, unless the caller accepts the loss."""
    policy, reason = CUBE_UNREADABLE_POLICIES[label]
    path = _policy_file(tmp_path, label, {**_POLICY_CUBE, "access_policy": policy})
    with pytest.raises(PolicyLossError) as exc:
        import_model("cube", path)
    message = str(exc.value)
    assert message.startswith("the access_policy of 1 cube (t) would NOT be imported in full")
    assert "(cubes.t: " in message and reason in message
    assert message.endswith(
        "the Cosmos model would grant more access than Cube did. Pass --allow-policy-loss to "
        "import anyway"
    )
    result = import_model("cube", path, allow_policy_loss=True)
    warning = next(w for w in result.warnings if "access_policy" in w)
    assert warning.startswith("cubes.t: access_policy not imported in full (")
    assert reason in warning
    assert "the Cosmos model grants more access than Cube did" in warning
    assert not result.project.semantic_models[0].row_filters


def test_cube_import_policy_members_and_claims(tmp_path):
    """A filter member may be plain or carry the cube's own name, CUBE, {CUBE} or ${CUBE};
    two security context attributes that would become one `$user` field are an error."""
    for prefix in ("", "t.", "CUBE.", "{CUBE}.", "${CUBE}."):
        policy = _row_level({**_REGION, "member": f"{prefix}region"})
        path = _policy_file(tmp_path, "members", {**_POLICY_CUBE, "access_policy": policy})
        result = import_model("cube", path)
        assert result.warnings == [], prefix
        filters = result.project.semantic_models[0].row_filters
        assert [f.filter for f in filters] == ["{region} = $user.region"], prefix
    upper = {**_REGION, "member": "secret", "values": ["{ securityContext.Region }"]}
    path = _policy_file(
        tmp_path, "case", {**_POLICY_CUBE, "access_policy": _row_level(_REGION, upper)}
    )
    with pytest.raises(ValueError, match=r"would both become `\$user.region`"):
        import_model("cube", path, allow_policy_loss=True)
    # Across cubes too.
    first = {**_POLICY_CUBE, "access_policy": _row_level(_REGION)}
    second = {**_POLICY_CUBE, "name": "u", "sql_table": "public.u"}
    second["access_policy"] = _row_level(upper)
    path = _policy_file(tmp_path, "cubes", first, second)
    with pytest.raises(ValueError, match=r"cubes.u: securityContext.Region and securityContext"):
        import_model("cube", path, allow_policy_loss=True)


def test_cube_export_refuses_exponent_numbers(graph):
    """Only plain numbers are exported, the set `cosmos import cube` reads back."""
    data = graph.project.model_dump(mode="json")
    payments = next(m for m in data["semantic_models"] if m["name"] == "payments")
    payments["row_filters"] = [{"name": "many", "filter": "{installments} > 1e1"}]
    with pytest.raises(PolicyLossError, match="it compares `installments` with `1e1`"):
        export_cube(Project.model_validate(data), allow_metric_loss=True)
    payments["row_filters"] = [{"name": "many", "filter": "{installments} > 10"}]
    exported = export_cube(Project.model_validate(data), allow_metric_loss=True)
    assert "- '10'" in exported.text


def test_cli_import_cube_policy_loss(tmp_path):
    runner = CliRunner()
    policy = [{"role": "manager", "row_level": {"filters": [_REGION]}}]
    path = _policy_file(tmp_path, "cli", {**_POLICY_CUBE, "access_policy": policy})
    out = tmp_path / "out.yml"
    refused = runner.invoke(app, ["import", "cube", str(path), "-o", str(out)])
    assert refused.exit_code == 1
    assert "would grant more access than Cube did" in refused.output
    assert not out.exists()
    flagged = runner.invoke(
        app, ["import", "cube", str(path), "-o", str(out), "--allow-policy-loss"]
    )
    assert flagged.exit_code == 0, flagged.output
    assert "warning: cubes.t: access_policy not imported in full" in flagged.output


def _cube_test_metrics(project: Project) -> Project:
    """The example without policies or cumulative metrics, plus `max` and `min` measures,
    filtered metrics of every aggregation, a filter on a dimension with an expression, and
    nested division."""
    data = _with_test_metrics(project).model_dump(mode="json")
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in (*CUMULATIVE, *OFFSETS)]
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
    assert len(names) == 33
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


def _cube_to_date_metrics(project: Project) -> Project:
    """The example without policies, plus a running total for every period over `sum` and
    `count` measures of two cubes."""
    data = _without_policies(_without_business(project))
    data["glossary"] = []
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in CUBE_LOST]
    for grain in ("day", "week", "month", "quarter", "year"):
        for measure in ("revenue", "order_count", "paid_value", "payment_count"):
            data["metrics"].append(
                {
                    "name": f"{grain}_to_date_{measure}",
                    "type": "cumulative",
                    "measure": measure,
                    "grain_to_date": grain,
                }
            )
    return Project.model_validate(data)


@pytest.mark.parametrize("source", SOURCES)
def test_cube_export_to_date_same_numbers(graph, tmp_path, source, request):
    """Every running total the export writes as a `to_date` rolling window returns the native
    numbers after the round trip, at every time grain, with and without another dimension, and
    with a filter on the time dimension."""
    connector = request.getfixturevalue(source)
    project = _cube_to_date_metrics(graph.project)
    full = compile_project(project)
    exported, back = _cube_round_trip(project, tmp_path)
    assert back.warnings == []
    measures = {
        m["name"]: m for c in yaml.safe_load(exported.text)["cubes"] for m in c.get("measures", [])
    }
    assert measures["week_to_date_payment_count"] == {
        "name": "week_to_date_payment_count",
        "type": "count",
        "rolling_window": {"type": "to_date", "granularity": "week"},
    }
    other = compile_project(back.project)
    names = [m.name for m in project.metrics if m.type == "cumulative" and m.grain_to_date]
    assert len(names) == 22
    # A literal date, not CURRENT_DATE: the Cube export drops the business time zone, so in the
    # evening in New York the re-imported model (UTC) is already on the next day.
    since = f"DATE '{(dt.date.today() - dt.timedelta(days=200)).isoformat()}'"
    refused = compared = 0
    for name in names:
        model = "payments" if name.endswith(("paid_value", "payment_count")) else "orders"
        time = "paid_at" if model == "payments" else "ordered_at"
        extra = "payments.method" if model == "payments" else "orders.status"
        for grain in ("day", "week", "month", "quarter", "year"):
            for dims, filters in (
                ([f"{model}.{time}__{grain}"], []),
                ([extra, f"{model}.{time}__{grain}"], []),
                # A start inside a period: both sides total from the first row kept.
                ([f"{model}.{time}__{grain}"], [f"{{{model}.{time}}} >= {since}"]),
            ):
                query = QueryRequest(metrics=[name], dimensions=dims, filters=filters)
                try:
                    native = rows(full, connector, query)
                except QueryError as refusal:
                    # A grain coarser than the period (DEC-21), or a week under a longer
                    # period (DEC-28), is refused, on both sides.
                    assert "cannot be grouped by" in str(refusal)
                    refused += 1
                    with pytest.raises(QueryError, match="cannot be grouped by"):
                        rows(other, connector, query)
                    continue
                assert native
                same(native, rows(other, connector, query))
                compared += 1
    assert refused and compared


# Rolling windows the Cube export writes as `trailing` windows (DEC-30): every aggregation a
# window combines, over measures of two cubes.
TRAILING_MEASURES = {
    "orders": ("revenue", "order_count", "smallest_order", "max_order_value"),
    "payments": ("paid_value", "payment_count"),
}


def _cube_trailing_metrics(project: Project) -> Project:
    """The example without policies, plus a 1, 7 and 30-day window over `sum`, `count`,
    `min` and `max` measures of two cubes."""
    data = _without_policies(_without_business(project))
    data["glossary"] = []
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in CUBE_LOST]
    for measures in TRAILING_MEASURES.values():
        for measure in measures:
            for days in (1, 7, 30):
                data["metrics"].append(
                    {
                        "name": f"rolling_{days}_{measure}",
                        "type": "cumulative",
                        "measure": measure,
                        "window": "1 day" if days == 1 else f"{days} days",
                    }
                )
    return Project.model_validate(data)


@pytest.mark.parametrize("source", SOURCES)
def test_cube_export_trailing_same_numbers(graph, tmp_path, source, request):
    """Every rolling window in days the export writes as a `trailing` window returns the native
    numbers after the round trip, by day, by day and another dimension,
    and with a filter on the time dimension that starts the range; other grains are
    refused on both sides."""
    connector = request.getfixturevalue(source)
    project = _cube_trailing_metrics(graph.project)
    full = compile_project(project)
    exported, back = _cube_round_trip(project, tmp_path)
    assert back.warnings == []
    measures = {
        m["name"]: m for c in yaml.safe_load(exported.text)["cubes"] for m in c.get("measures", [])
    }
    assert measures["rolling_30_payment_count"] == {
        "name": "rolling_30_payment_count",
        "type": "count",
        "rolling_window": {"trailing": "30 day", "offset": "end"},
    }
    assert measures["rolling_1_max_order_value"]["rolling_window"] == {
        "trailing": "1 day",
        "offset": "end",
    }
    other = compile_project(back.project)
    names = [m.name for m in project.metrics if m.type == "cumulative" and m.window]
    assert len(names) == 21
    for name in names:
        assert other.metrics[name].window == full.metrics[name].window
    # A literal date, not CURRENT_DATE (see the running totals above).
    since = f"DATE '{(dt.date.today() - dt.timedelta(days=200)).isoformat()}'"
    refused = compared = 0
    for model, measure_names in TRAILING_MEASURES.items():
        time = "paid_at" if model == "payments" else "ordered_at"
        extra = "payments.method" if model == "payments" else "orders.status"
        for measure in measure_names:
            for days in (1, 7, 30):
                name = f"rolling_{days}_{measure}"
                for dims, filters in (
                    ([f"{model}.{time}__day"], []),
                    ([extra, f"{model}.{time}__day"], []),
                    # A filter on the time dimension: both sides fill the first windows of
                    # the range from the days before it (DEC-34).
                    ([f"{model}.{time}__day"], [f"{{{model}.{time}}} >= {since}"]),
                ):
                    query = QueryRequest(metrics=[name], dimensions=dims, filters=filters)
                    native = rows(full, connector, query)
                    assert native
                    same(native, rows(other, connector, query))
                    compared += 1
                for grain in ("week", "month"):
                    query = QueryRequest(metrics=[name], dimensions=[f"{model}.{time}__{grain}"])
                    for side in (full, other):
                        with pytest.raises(QueryError, match="needs the time dimension at day"):
                            rows(side, connector, query)
                    refused += 1
    assert compared == 54 and refused == 36


@pytest.mark.parametrize(
    ("window", "kind", "metric", "warning"),
    [
        ({"trailing": "7 day", "offset": "end"}, "sum", {"window": "7 days"}, None),
        # `offset` defaults to `end`.
        ({"trailing": "7 days"}, "min", {"window": "7 days"}, None),
        ({"trailing": "1 day", "offset": "end"}, "count", {"window": "1 day"}, None),
        ({"type": "fixed", "trailing": "30 day"}, "max", {"window": "30 days"}, None),
        ({"trailing": "7 day", "offset": "start"}, "sum", None, "does not end on the row's day"),
        ({"trailing": "7 day", "leading": "1 day"}, "sum", None, "does not end on the row's day"),
        ({"trailing": "7 day"}, "avg", None, "of type `avg` cannot be combined"),
        (
            {"trailing": "7 day"},
            "count_distinct",
            None,
            "of type `count_distinct` cannot be combined",
        ),
        ({"trailing": "1 month"}, "sum", {"window": "1 month"}, "only rolling windows in days"),
    ],
)
def test_cube_import_trailing_windows(tmp_path, window, kind, metric, warning):
    """A trailing window in days ending at the bucket's end, over a measure whose daily values
    combine, is a Cosmos window in days and validates; one that does not end on the row's day,
    or over an `avg` or `count_distinct` measure, is skipped with its base measure; other units
    stay refused at query time."""
    doc = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "public.orders",
                "dimensions": [
                    {"name": "order_id", "sql": "{CUBE}.order_id", "type": "string"},
                    {"name": "ordered_at", "sql": "{CUBE}.ordered_at", "type": "time"},
                ],
                "measures": [
                    {
                        "name": "revenue_window",
                        "type": kind,
                        "sql": "{CUBE}.amount",
                        "rolling_window": window,
                    }
                ],
            }
        ]
    }
    path = tmp_path / "model.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    result = import_model("cube", path)
    metrics = {m.name: m for m in result.project.metrics}
    measures = {x.name for m in result.project.semantic_models for x in m.measures}
    if metric is None:
        # Skipped whole: no metric, and no `__base` measure left behind.
        assert "revenue_window" not in metrics
        assert measures == set()
    else:
        assert metrics["revenue_window"].window == metric["window"]
        assert metrics["revenue_window"].measure == "revenue_window__base"
        assert measures == {"revenue_window__base"}
    if warning is None:
        assert result.warnings == []
        compile_project(result.project)  # validates
    else:
        assert [w for w in result.warnings if warning in w], result.warnings


# A running total of an average or a distinct count cannot be accumulated from per-period
# values, so model validation refuses it (DEC-21). The Cube and dbt imports skip it with a
# warning naming it, with the metrics that use it, so the imported model validates (DEC-32).


@pytest.mark.parametrize("kind", ["avg", "count_distinct"])
def test_cube_import_skips_to_date_over_avg_or_distinct_count(tmp_path, kind):
    doc = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "public.orders",
                "dimensions": [
                    {"name": "order_id", "sql": "{CUBE}.order_id", "type": "string"},
                    {"name": "ordered_at", "sql": "{CUBE}.ordered_at", "type": "time"},
                ],
                "measures": [
                    {"name": "revenue", "type": "sum", "sql": "{CUBE}.amount"},
                    {
                        "name": "revenue_ytd",
                        "type": "sum",
                        "sql": "{CUBE}.amount",
                        "rolling_window": {"type": "to_date", "granularity": "year"},
                    },
                    {
                        "name": "value_mtd",
                        "type": kind,
                        "sql": "{CUBE}.amount",
                        "rolling_window": {"type": "to_date", "granularity": "month"},
                    },
                    # Use the skipped measure, directly and through another metric.
                    {"name": "value_share", "type": "number", "sql": "{value_mtd} / {revenue}"},
                    {"name": "value_share_pct", "type": "number", "sql": "100 * {value_share}"},
                ],
            }
        ]
    }
    path = tmp_path / "model.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    graph, warnings = imported("cube", path)  # dumps, reloads and validates the model
    assert set(graph.metrics) == {"revenue_ytd"}
    assert graph.metrics["revenue_ytd"].grain_to_date == "year"
    measures = {x.name for m in graph.project.semantic_models for x in m.measures}
    assert measures == {"revenue", "revenue_ytd__base"}  # no `value_mtd__base` left behind
    where = "cubes.orders.measures"
    assert warnings == [
        f"{where}.value_mtd: a to-date window over a measure of type `{kind}` cannot be "
        "accumulated from per-period values in Cosmos (use sum, count, min or max); an average "
        "or a distinct count to date must be computed over the whole period; measure skipped",
        f"{where}.value_share: depends on `value_mtd`, which was not imported; skipped",
        f"{where}.value_share_pct: depends on `value_share`, which was not imported; skipped",
    ]
    runner = CliRunner()
    out = tmp_path / "imported.yml"
    assert runner.invoke(app, ["import", "cube", str(path), "-o", str(out)]).exit_code == 0
    validated = runner.invoke(app, ["validate", "-m", str(out)])
    assert validated.exit_code == 0, validated.output


@pytest.mark.parametrize("spec", ["legacy", "latest"])
@pytest.mark.parametrize("agg", ["average", "count_distinct"])
def test_dbt_import_skips_to_date_over_avg_or_distinct_count(tmp_path, spec, agg):
    if spec == "legacy":
        doc = {
            "semantic_models": [
                {
                    "name": "orders",
                    "model": "ref('orders')",
                    "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                    "dimensions": [
                        {"name": "ordered_at", "type": "time"},
                        {"name": "status", "type": "categorical"},
                    ],
                    "measures": [
                        {"name": "revenue", "agg": "sum", "expr": "amount"},
                        {"name": "value", "agg": agg, "expr": "amount"},
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "revenue_ytd",
                    "type": "cumulative",
                    "type_params": {"measure": "revenue", "grain_to_date": "year"},
                },
                {
                    "name": "value_mtd",
                    "type": "cumulative",
                    "type_params": {"measure": "value", "grain_to_date": "month"},
                },
                {
                    "name": "value_share",
                    "type": "ratio",
                    "type_params": {"numerator": "value_mtd", "denominator": "revenue_ytd"},
                },
                {
                    "name": "value_share_pct",
                    "type": "derived",
                    "type_params": {
                        "expr": "100 * share",
                        "metrics": [{"name": "value_share", "alias": "share"}],
                    },
                },
            ],
        }
    else:
        doc = {
            "models": [
                {
                    "name": "orders",
                    "semantic_model": True,
                    "columns": [
                        {"name": "order_id", "entity": {"type": "primary", "name": "order"}},
                        {"name": "ordered_at", "dimension": {"type": "time"}},
                    ],
                    "metrics": [
                        {"name": "revenue", "type": "simple", "agg": "sum", "expr": "amount"},
                        {"name": "value", "type": "simple", "agg": agg, "expr": "amount"},
                        {
                            "name": "revenue_ytd",
                            "type": "cumulative",
                            "input_metric": "revenue",
                            "grain_to_date": "year",
                        },
                        {
                            "name": "value_mtd",
                            "type": "cumulative",
                            "input_metric": "value",
                            "grain_to_date": "month",
                        },
                        {
                            "name": "value_share",
                            "type": "ratio",
                            "numerator": "value_mtd",
                            "denominator": "revenue_ytd",
                        },
                        {
                            "name": "value_share_pct",
                            "type": "derived",
                            "expr": "100 * value_share",
                            "input_metrics": ["value_share"],
                        },
                    ],
                }
            ]
        }
    path = tmp_path / "dbt" / "models.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    graph, warnings = imported("dbt", path.parent, schema="public")
    assert set(graph.metrics) == {"revenue_ytd"}
    assert graph.metrics["revenue_ytd"].grain_to_date == "year"
    assert "value" in graph.measure_owner  # the measure itself is kept
    cosmos_agg = "avg" if agg == "average" else agg
    assert warnings == [
        "metrics.value_mtd: over measure `value`: a month-to-date over a "
        f"`{cosmos_agg}` measure is not supported; it cannot be accumulated from per-period "
        "values (use a sum, count, min or max measure); metric skipped",
        "metrics.value_share: depends on `value_mtd`, which was not imported; skipped",
        "metrics.value_share_pct: depends on `value_share`, which was not imported; skipped",
    ]
    runner = CliRunner()
    out = tmp_path / "imported.yml"
    imported_cli = runner.invoke(
        app, ["import", "dbt", str(path.parent), "-o", str(out), "--schema", "public"]
    )
    assert imported_cli.exit_code == 0, imported_cli.output
    validated = runner.invoke(app, ["validate", "-m", str(out)])
    assert validated.exit_code == 0, validated.output


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
        {
            "name": "ytd_share",
            "type": "ratio",
            "numerator": "revenue_year_to_date",
            "denominator": "revenue",
        },
        # A running maximum is valid, but Cube carries only `sum` and `count` running totals.
        {"name": "max_order_mtd", "type": "cumulative", "measure": "max_order_value"},
        {"name": "revenue_running", "type": "cumulative", "measure": "revenue"},
        {"name": "rolling_gap", "type": "derived", "expr": "{revenue_rolling_7_days} - {revenue}"},
        {
            "name": "rolling_share",
            "type": "ratio",
            "numerator": "revenue_rolling_7_days",
            "denominator": "revenue",
        },
    ]
    data["metrics"][-4]["grain_to_date"] = "month"
    project = Project.model_validate(data)
    with pytest.raises(MetricLossError) as exc:
        export_cube(project)
    assert str(exc.value).startswith(
        "16 metrics (max_order_value_year_to_date, revenue_prev_month, revenue_mom_growth, "
        "revenue_yoy_growth, order_count_7_days_earlier, midwest_revenue, "
    )
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
    # The running totals themselves are exported; the metrics over them are not, because the
    # planner does not compute a cumulative metric inside another one.
    assert "metrics.revenue_year_to_date" not in reasons
    for name in ("ytd_gap", "ytd_share"):
        assert reasons[f"metrics.{name}"].startswith(
            "it uses the cumulative metric `revenue_year_to_date`, and Cosmos does not compute"
        )
    assert "metrics.revenue_rolling_7_days" not in reasons
    for name in ("rolling_gap", "rolling_share"):
        assert reasons[f"metrics.{name}"].startswith(
            "it uses the cumulative metric `revenue_rolling_7_days`, and Cosmos does not compute"
        )
    for name in ("max_order_mtd", "max_order_value_year_to_date"):
        assert reasons[f"metrics.{name}"].startswith(
            "it is cumulative over a measure that aggregates with `max`, and the Cube export "
            "carries running totals of `sum` and `count` measures only"
        )
    assert reasons["metrics.revenue_running"].startswith("it is cumulative without `grain_to_date`")
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
    # The example's policies are exported: only the metrics Cube cannot carry are refused.
    refused = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE)])
    assert refused.exit_code == 1
    assert refused.stdout == ""
    assert "row filter" not in refused.stderr
    assert (
        "5 metrics (max_order_value_year_to_date, revenue_prev_month, revenue_mom_growth, "
        "revenue_yoy_growth, order_count_7_days_earlier) would NOT be exported" in refused.stderr
    )
    flags = ["--allow-metric-loss"]
    result = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE), *flags])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout)["cubes"]
    assert "warning:" not in result.stdout
    assert "warning: metrics.collection_gap: it spans cubes" in result.stderr
    out = tmp_path / "model" / "cubes.yml"
    result = runner.invoke(app, ["export", "cube", "-m", str(EXAMPLE), "-o", str(out), *flags])
    assert result.exit_code == 0, result.output
    assert "14 warnings" in result.stdout
    assert import_model("cube", out).warnings == []


# -- dbt export (semantic models, simple, ratio and derived metrics) ----------------------------


def _dbt_round_trip(project: Project, tmp_path: Path, **options):
    exported = export_dbt(project, **options)
    path = tmp_path / "dbt" / "semantic_models.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(exported.text, encoding="utf-8")
    return exported, import_model("dbt", path, schema="public")


def _dbt_test_metrics(project: Project) -> Project:
    """The Cube test model plus the shapes dbt carries and Cube does not: a filter on another
    model's dimension and a ratio with a filter."""
    data = _cube_test_metrics(project).model_dump(mode="json")
    data["metrics"] += [
        {
            "name": "midwest_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{customers.region} = 'Midwest'",
        },
        {
            "name": "completed_rate",
            "type": "ratio",
            "numerator": "revenue",
            "denominator": "order_count",
            "filter": "{status} = 'completed'",
        },
    ]
    return Project.model_validate(data)


def test_dbt_export_fails_closed(graph):
    """The example has row filters, sensitivity and a running maximum: each loss is refused
    until the caller accepts it. Its offsets (DEC-36) and its running totals of sums (DEC-31)
    are exported."""
    with pytest.raises(PolicyLossError) as exc:
        export_dbt(graph.project, allow_metric_loss=True)
    assert str(exc.value) == (
        "1 row filter (customers.by_region) and the sensitivity of 3 dimensions "
        "(customers.email, customers.tax_id, customers.health_plan) would NOT be exported: the "
        "dbt export does not carry access or personal-data policies yet; the dbt model would "
        "not have this row-level security or personal-data marking. Pass --allow-policy-loss to "
        "export anyway"
    )
    with pytest.raises(MetricLossError) as exc:
        export_dbt(graph.project, allow_policy_loss=True)
    assert str(exc.value) == (
        "1 metric (max_order_value_year_to_date) would NOT be exported "
        "(max_order_value_year_to_date: it is a running total over a measure that aggregates "
        "with `max`, and the dbt export carries running totals of `sum` and `count` measures "
        "only); "
        "the dbt model would not have them. Pass --allow-metric-loss to export anyway"
    )


def test_dbt_export_round_trip(graph, tmp_path):
    exported, back = _dbt_round_trip(graph.project, tmp_path, **LOSS_FLAGS)
    # The importer reads the whole export; it reports only the rolling windows' and the
    # offsets' conditions.
    differences = (
        "Cosmos moves the start of the query's period, filters the facts before the shift and "
        "returns only periods with facts; MetricFlow joins a time spine, so it also returns "
        "periods without facts whose earlier period has some, and it applies filters on "
        "`metric_time` after the shift"
    )
    assert back.warnings == [
        *(
            f"metrics.{name}: rolling window `{days} days`: only windows in days, queried by "
            "day, are supported, and only days with data appear (MetricFlow uses a time spine)"
            for name, days in zip(ROLLING, (30, 7, 7), strict=True)
        ),
        "metrics.revenue_prev_month: `revenue` is read `1 month` earlier (`offset_window: 1 "
        f"month`), queried by month. {differences}",
        # dbt's aliased input of a metric also read unshifted comes back as a helper metric.
        "metrics.revenue_yoy_growth__revenue_year_before: `revenue` is read `12 months` earlier "
        f"(`offset_window: 1 year`), queried by month or quarter or year. {differences}",
        "metrics.order_count_7_days_earlier: `order_count` is read `7 days` earlier "
        f"(`offset_window: 1 week`), queried by day or week. {differences}",
    ]
    doc = yaml.safe_load(exported.text)
    models = {m["name"]: m for m in doc["semantic_models"]}
    orders = models["orders"]
    assert orders["model"] == "ref('orders')"
    assert orders["defaults"] == {"agg_time_dimension": "ordered_at"}
    assert orders["entities"][1] == {"name": "customer", "type": "foreign", "expr": "customer_id"}
    assert orders["measures"][0]["expr"] == "1"
    assert all(m["create_metric"] for s in doc["semantic_models"] for m in s.get("measures", []))
    metrics = {m["name"]: m for m in doc["metrics"]}
    assert not set(metrics) & set(DBT_LOST)
    assert metrics["revenue_rolling_7_days"]["type"] == "cumulative"
    assert metrics["revenue_rolling_7_days"]["type_params"] == {
        "measure": "revenue",
        "cumulative_type_params": {"window": "7 days"},
    }
    # A running total since the start of the period, kept per bucket at its last day (DEC-31).
    assert metrics["revenue_year_to_date"]["type_params"] == {
        "measure": "revenue",
        "cumulative_type_params": {"grain_to_date": "year", "period_agg": "last"},
    }
    assert metrics["revenue_month_to_date"]["type_params"]["cumulative_type_params"] == {
        "grain_to_date": "month",
        "period_agg": "last",
    }
    assert metrics["completed_revenue"]["filter"] == (
        "{{ Dimension('order__status') }} = 'completed'"
    )
    assert metrics["orders_per_buyer"]["type_params"] == {
        "expr": "(1e0 * order_count / NULLIF(buyers, 0))",
        "metrics": [{"name": "order_count"}, {"name": "buyers"}],
    }
    # Offsets are MetricFlow's `offset_window`, in the largest unit that holds them (DEC-36).
    assert metrics["revenue_prev_month"]["type_params"] == {
        "expr": "revenue",
        "metrics": [{"name": "revenue", "offset_window": "1 month"}],
    }
    assert metrics["order_count_7_days_earlier"]["type_params"] == {
        "expr": "order_count",
        "metrics": [{"name": "order_count", "offset_window": "1 week"}],
    }
    assert metrics["revenue_mom_growth"]["type_params"] == {
        "expr": "(1e0 * (revenue - revenue_prev_month) / NULLIF(revenue_prev_month, 0))",
        "metrics": [{"name": "revenue"}, {"name": "revenue_prev_month"}],
    }
    # An aliased offset input (DEC-44) is MetricFlow's input `alias`.
    assert metrics["revenue_yoy_growth"]["type_params"] == {
        "expr": "(1e0 * (revenue - revenue_year_before) / NULLIF(revenue_year_before, 0))",
        "metrics": [
            {"name": "revenue"},
            {"name": "revenue", "offset_window": "1 year", "alias": "revenue_year_before"},
        ],
    }

    # Models, tables, owners, entities, dimensions and measures come back as they were (AVG
    # with a `1e0 *` factor, so it is real in dbt too).
    def shape(project: Project) -> dict:
        return {
            m.name: (
                m.table,
                m.owner,
                m.description,
                [e.model_dump() for e in m.entities],
                [(d.name, d.type, d.expr, d.description) for d in m.dimensions],
                [(x.name, x.agg, x.expr, x.description) for x in m.measures],
            )
            for m in project.semantic_models
        }

    want = shape(Project.model_validate(_without_policies(graph.project.model_dump(mode="json"))))
    avg = want["order_items"][5][2]
    want["order_items"][5][2] = (avg[0], avg[1], "1e0 * quantity", avg[3])
    assert shape(back.project) == want

    # Every other metric comes back with its type, owner and description; dbt requires a
    # label, so a metric without one gets its name. Filters name the model; division is real.
    def metric_shape(project: Project) -> dict:
        out = {}
        for m in project.metrics:
            data = m.model_dump(exclude_none=True)
            if data.get("label") == m.name:
                del data["label"]
            out[m.name] = data
        return out

    want = {k: v for k, v in metric_shape(graph.project).items() if k not in DBT_LOST}
    want["completed_revenue"]["filter"] = "{orders.status} = 'completed'"
    want["orders_per_buyer"]["expr"] = "(1e0 * {order_count} / NULLIF({buyers}, 0))"
    want["items_per_order"]["expr"] = "(1e0 * {items_sold} / NULLIF({order_count}, 0))"
    want["revenue_mom_growth"]["expr"] = (
        "(1e0 * ({revenue} - {revenue_prev_month}) / NULLIF({revenue_prev_month}, 0))"
    )
    assert want["revenue_prev_month"]["inputs"] == [{"metric": "revenue", "offset": "1 month"}]
    # The import reads dbt's aliased input of a metric also read unshifted as a helper metric
    # (DEC-36), with the same numbers (test_dbt_export_same_numbers).
    helper = "revenue_yoy_growth__revenue_year_before"
    want["revenue_yoy_growth"]["expr"] = (
        f"(1e0 * ({{revenue}} - {{{helper}}}) / NULLIF({{{helper}}}, 0))"
    )
    want["revenue_yoy_growth"]["inputs"] = []
    want[helper] = {
        "name": helper,
        "type": "derived",
        "expr": "{revenue}",
        "inputs": [{"metric": "revenue", "offset": "12 months"}],
    }
    assert metric_shape(back.project) == want

    # Every loss is a warning: policies, a running maximum, the schema, and what a later slice
    # exports; each rolling window and running total states where MetricFlow's numbers are the
    # engine's.
    joined = "\n".join(exported.warnings)
    assert "semantic_models.customers.row_filters.by_region: not exported to dbt" in joined
    assert sum("its sensitivity is not exported" in w for w in exported.warnings) == 3
    for name in RUNNING:
        assert f"metrics.{name}: it is a running total over a measure" in joined
    assert (
        "metrics.revenue_month_to_date: MetricFlow accumulates over `metric_time`, which is "
        "`orders.ordered_at`; by that dimension at day or month grain the numbers are the "
        "engine's on the periods with facts. MetricFlow also returns periods without facts after "
        "the first fact of a month and the grains the engine refuses, and a time constraint that "
        "starts inside a month does not cut its total, while the engine filters the facts first"
    ) in exported.warnings
    assert (
        "metrics.revenue_year_to_date: MetricFlow accumulates over `metric_time`, which is "
        "`orders.ordered_at`; by that dimension at day, month, quarter or year grain"
    ) in joined
    assert (
        "metrics.revenue_rolling_7_days: MetricFlow windows over `metric_time`, which is "
        "`orders.ordered_at`; by that dimension at day grain the numbers are the engine's on the "
        "days with facts. MetricFlow also returns days without facts inside a window and coarser "
        "grains (the engine refuses them)"
    ) in exported.warnings
    assert (
        "(the engine refuses them); queried by `customers.last_order_at`, the engine "
        "windows over that dimension instead, which MetricFlow does not"
    ) in joined
    # Each offset states where MetricFlow's numbers are the engine's.
    assert (
        "metrics.revenue_prev_month: MetricFlow shifts `revenue` by `1 month` (`offset_window: "
        "1 month`) on `metric_time`, which is `orders.ordered_at`; by that dimension at the "
        "grains the engine accepts (month) the periods both return have the same numbers. "
        "MetricFlow joins a time spine, so it also returns periods without facts whose earlier "
        "period has some, and grains the engine refuses; it applies filters on `metric_time` "
        "after the shift, while the engine filters the facts first, so a time filter leaves out "
        "the earlier periods in the engine only"
    ) in exported.warnings
    assert (
        "metrics.order_count_7_days_earlier: MetricFlow shifts `order_count` by `7 days` "
        "(`offset_window: 1 week`) on `metric_time`, which is `orders.ordered_at`; by that "
        "dimension at the grains the engine accepts (day, week) the periods"
    ) in joined
    assert (
        "metrics.revenue_yoy_growth: MetricFlow shifts `revenue` (as `revenue_year_before`) by "
        "`12 months` (`offset_window: 1 year`) on `metric_time`, which is `orders.ordered_at`; "
        "by that dimension at the grains the engine accepts (month, quarter, year) the periods"
    ) in joined
    assert "metrics.revenue_mom_growth" not in joined
    assert "import with `cosmos import dbt --schema public`" in joined
    for name in ("products", "order_items"):
        assert f"semantic_models.{name}: it has measures but no time dimension" in joined
    for key in ("business_rules", "glossary", "teams", "systems", "processes"):
        assert f"{key}: not exported to dbt yet" in joined
    assert "user_context: region" in joined
    assert "not America/New_York" in joined
    assert len(exported.warnings) == 23


@pytest.mark.parametrize("source", SOURCES)
def test_dbt_export_same_numbers(graph, tmp_path, source, request):
    """Exported to dbt and imported back, the model answers the reference queries with the
    hand-written SQL's numbers, on every database available."""
    connector = request.getfixturevalue(source)
    _, back = _dbt_round_trip(graph.project, tmp_path, **LOSS_FLAGS)
    other = compile_project(back.project)
    cases = _cube_cases(graph.project, policies=False, lost=DBT_LOST)
    # The rolling windows, the running totals (DEC-31): year-to-date by month (A08) and
    # month-to-date by status and day from 45 days ago (A09), and the offsets (DEC-36).
    assert {"A08", "A09", "A29", "A30", "A34", "A35", "A36", "A37", "A49", "A51"} <= {
        c.id for c in cases
    }
    assert len(cases) == 40
    for case in cases:
        result = run_case(case, other, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"
    # With the original policies put back, the queries that read the user pass too, and the
    # running totals are refused where the native model refuses them: month-to-date by quarter
    # (A32), year-to-date by week (A39) and with a metric filter (A42).
    governed = compile_project(_with_policies(back.project, graph.project))
    cases = _cube_cases(graph.project, policies=True, lost=DBT_LOST)
    assert {"R17", "R18", "A32", "A33", "A38", "A39", "A40", "A41", "A42"} <= {c.id for c in cases}
    assert len(cases) == 64
    for case in cases:
        result = run_case(case, governed, connector)
        assert result.status == "pass", f"{case.id}: {result.detail}"


@pytest.mark.parametrize("source", SOURCES)
def test_dbt_export_metric_shapes_same_numbers(graph, tmp_path, source, request):
    """Every measure and metric the export writes returns the native numbers after the round
    trip, alone and by order status."""
    connector = request.getfixturevalue(source)
    project = _dbt_test_metrics(graph.project)
    full = compile_project(project)
    exported, back = _dbt_round_trip(project, tmp_path)
    assert back.warnings == []
    metrics = {m["name"]: m for m in yaml.safe_load(exported.text)["metrics"]}
    assert metrics["midwest_revenue"]["filter"] == (
        "{{ Dimension('customer__region') }} = 'Midwest'"
    )
    assert metrics["discounted_revenue"]["filter"] == (
        "{{ Dimension('order__has_discount') }} AND {{ Dimension('order__status') }} <> 'canceled'"
    )
    assert metrics["share_of_net"]["type_params"]["expr"] == (
        "(1e0 * net_revenue / NULLIF(((1e0 * revenue / NULLIF(2, 0))), 0))"
    )
    other = compile_project(back.project)
    names = [*full.measure_owner, *(m.name for m in project.metrics)]
    assert len(names) == 35
    for name in names:
        for dims in ([], ["orders.status"]):
            query = QueryRequest(metrics=[name], dimensions=dims)
            if dims and full.measure_owner.get(name) in ("customers", "products", "sellers"):
                continue  # those models do not reach orders
            same(rows(full, connector, query), rows(other, connector, query))
    # The boolean dimension keeps its type (SQL Server selects it through a CASE).
    query = QueryRequest(metrics=["revenue"], dimensions=["orders.has_discount"])
    same(rows(full, connector, query), rows(other, connector, query))


def _dbt_window_metrics(project: Project) -> Project:
    """The example without policies, plus rolling windows of 1, 3, 7 and 30 days over `sum`,
    `count`, `min` and `max` measures of two models."""
    data = _without_policies(_without_business(project))
    data["glossary"] = []
    lost = (*DBT_LOST, *ROLLING, *TO_DATE, *OFFSETS)
    data["metrics"] = [m for m in data["metrics"] if m["name"] not in lost]
    for days in (1, 3, 7, 30):
        for measure in (
            "revenue",
            "order_count",
            "smallest_order",
            "max_order_value",
            "paid_value",
            "payment_count",
        ):
            data["metrics"].append(
                {
                    "name": f"{measure}_{days}d",
                    "type": "cumulative",
                    "measure": measure,
                    "window": "1 day" if days == 1 else f"{days} days",
                }
            )
    return Project.model_validate(data)


@pytest.mark.parametrize("source", SOURCES)
def test_dbt_export_rolling_windows_same_numbers(graph, tmp_path, source, request):
    """Every rolling window the export writes comes back with the native numbers, by day, by
    day and another dimension, and with a filter on the time dimension; both models refuse
    the other grains."""
    connector = request.getfixturevalue(source)
    project = _dbt_window_metrics(graph.project)
    full = compile_project(project)
    exported, back = _dbt_round_trip(project, tmp_path)
    metrics = {m["name"]: m for m in yaml.safe_load(exported.text)["metrics"]}
    assert metrics["payment_count_1d"] == {
        "name": "payment_count_1d",
        "label": "payment_count_1d",
        "type": "cumulative",
        "type_params": {"measure": "payment_count", "cumulative_type_params": {"window": "1 day"}},
    }
    names = [m.name for m in project.metrics if m.type == "cumulative"]
    assert len(names) == 24
    back_metrics = {m.name: m for m in back.project.metrics}
    for name in names:
        assert back_metrics[name].window == full.metrics[name].window
        assert back_metrics[name].measure == full.metrics[name].measure
    assert len(back.warnings) == len(names)
    other = compile_project(back.project)
    since = f"DATE '{(dt.date.today() - dt.timedelta(days=200)).isoformat()}'"
    refused = compared = 0
    for name in names:
        model = "payments" if name.startswith(("paid_value", "payment_count")) else "orders"
        time = "paid_at" if model == "payments" else "ordered_at"
        extra = "payments.method" if model == "payments" else "orders.status"
        for grain in ("day", "week", "month"):
            for dims, filters in (
                ([f"{model}.{time}__{grain}"], []),
                ([extra, f"{model}.{time}__{grain}"], []),
                # Both filter the facts first, so the window starts at the first day kept.
                ([f"{model}.{time}__{grain}"], [f"{{{model}.{time}}} >= {since}"]),
            ):
                query = QueryRequest(metrics=[name], dimensions=dims, filters=filters)
                try:
                    native = rows(full, connector, query)
                except QueryError as refusal:
                    assert grain != "day"
                    assert "needs the time dimension at day grain" in str(refusal)
                    refused += 1
                    with pytest.raises(QueryError, match="needs the time dimension at day grain"):
                        rows(other, connector, query)
                    continue
                assert native
                same(native, rows(other, connector, query))
                compared += 1
    assert compared == 3 * len(names)
    assert refused == 6 * len(names)


@pytest.mark.parametrize("source", SOURCES)
def test_dbt_export_to_date_same_numbers(graph, tmp_path, source, request):
    """Every running total the export writes as a `grain_to_date` cumulative metric (DEC-31)
    returns the native numbers after the round trip, for every period over `sum` and `count`
    measures of two models, at every time grain, with and without another dimension, and with
    a filter on the time dimension; both models refuse the same grains."""
    connector = request.getfixturevalue(source)
    project = _cube_to_date_metrics(graph.project)
    full = compile_project(project)
    exported, back = _dbt_round_trip(project, tmp_path)
    # The importer reports only the rolling windows' conditions.
    assert len(back.warnings) == len(ROLLING)
    assert all("rolling window" in w for w in back.warnings)
    metrics = {m["name"]: m for m in yaml.safe_load(exported.text)["metrics"]}
    assert metrics["week_to_date_payment_count"] == {
        "name": "week_to_date_payment_count",
        "label": "week_to_date_payment_count",
        "type": "cumulative",
        "type_params": {
            "measure": "payment_count",
            "cumulative_type_params": {"grain_to_date": "week", "period_agg": "last"},
        },
    }
    names = [m.name for m in project.metrics if m.type == "cumulative" and m.grain_to_date]
    assert len(names) == 22
    back_metrics = {m.name: m for m in back.project.metrics}
    for name in names:
        assert back_metrics[name].grain_to_date == full.metrics[name].grain_to_date
        assert back_metrics[name].measure == full.metrics[name].measure
        assert back_metrics[name].window is None
    # Each one states the grains where MetricFlow's numbers are the engine's.
    conditions = {w.split(": ", 1)[0]: w for w in exported.warnings}
    for name, grains in (
        ("day_to_date_revenue", "at day grain"),
        ("week_to_date_revenue", "at day or week grain"),
        ("month_to_date_paid_value", "at day or month grain"),
        ("quarter_to_date_order_count", "at day, month or quarter grain"),
        ("year_to_date_payment_count", "at day, month, quarter or year grain"),
    ):
        assert f"by that dimension {grains} the numbers" in conditions[f"metrics.{name}"]
    assert "`payments.paid_at`" in conditions["metrics.year_to_date_payment_count"]
    other = compile_project(back.project)
    # A literal date, not CURRENT_DATE, as in the Cube test.
    since = f"DATE '{(dt.date.today() - dt.timedelta(days=200)).isoformat()}'"
    refused = compared = 0
    for name in names:
        model = "payments" if name.endswith(("paid_value", "payment_count")) else "orders"
        time = "paid_at" if model == "payments" else "ordered_at"
        extra = "payments.method" if model == "payments" else "orders.status"
        for grain in ("day", "week", "month", "quarter", "year"):
            for dims, filters in (
                ([f"{model}.{time}__{grain}"], []),
                ([extra, f"{model}.{time}__{grain}"], []),
                # A start inside a period: both sides total from the first row kept.
                ([f"{model}.{time}__{grain}"], [f"{{{model}.{time}}} >= {since}"]),
            ):
                query = QueryRequest(metrics=[name], dimensions=dims, filters=filters)
                try:
                    native = rows(full, connector, query)
                except QueryError as refusal:
                    # A grain coarser than the period (DEC-21), or a week under a longer
                    # period (DEC-28), is refused, on both sides.
                    assert "cannot be grouped by" in str(refusal)
                    refused += 1
                    with pytest.raises(QueryError, match="cannot be grouped by"):
                        rows(other, connector, query)
                    continue
                assert native
                same(native, rows(other, connector, query))
                compared += 1
    assert refused and compared
    assert refused + compared == 15 * len(names)


# Offsets the dbt export writes (DEC-36), with MetricFlow's `offset_window` for each and the
# grains the engine accepts for it.
DBT_OFFSETS = {
    "1 day": ("1 day", ("day",)),
    "7 days": ("1 week", ("day", "week")),
    "14 days": ("2 weeks", ("day", "week")),
    "1 month": ("1 month", ("month",)),
    "3 months": ("1 quarter", ("month", "quarter")),
    "12 months": ("1 year", ("month", "quarter", "year")),
}
GRAINS = ("day", "week", "month", "quarter", "year")


def _dbt_base(project: Project) -> dict:
    """The example without policies, running totals, rolling windows and offsets: a model the
    dbt export carries whole."""
    data = _without_policies(_without_business(project))
    data["glossary"] = []
    data["metrics"] = [
        m for m in data["metrics"] if m["name"] not in (*DBT_LOST, *ROLLING, *OFFSETS)
    ]
    return data


def _dbt_offset_metrics(project: Project) -> Project:
    """The base model plus every offset of ``DBT_OFFSETS`` over `sum` and `count` measures of
    two models, a growth over a shifted metric and a shifted ratio."""
    data = _dbt_base(project)
    for offset in DBT_OFFSETS:
        tag = offset.replace(" ", "_")
        for measure in ("revenue", "order_count", "paid_value", "payment_count"):
            data["metrics"].append(
                {
                    "name": f"{measure}_{tag}",
                    "type": "derived",
                    "expr": "{" + measure + "}",
                    "inputs": [{"metric": measure, "offset": offset}],
                }
            )
    data["metrics"] += [
        {
            "name": "revenue_yoy",
            "type": "derived",
            "expr": "({revenue} - {revenue_12_months}) / {revenue_12_months}",
        },
        {
            "name": "orders_per_buyer_1_month",
            "type": "derived",
            "expr": "{orders_per_buyer} * 100",
            "inputs": [{"metric": "orders_per_buyer", "offset": "1 month"}],
        },
    ]
    return Project.model_validate(data)


@pytest.mark.parametrize("source", SOURCES)
def test_dbt_export_offsets_same_numbers(graph, tmp_path, source, request):
    """Every offset the export writes as `offset_window` comes back as the same offset, with
    the native numbers at every grain the engine accepts, by another dimension too and with a
    filter on the time dimension; both models refuse the other grains with the same error."""
    connector = request.getfixturevalue(source)
    project = _dbt_offset_metrics(graph.project)
    full = compile_project(project)
    exported, back = _dbt_round_trip(project, tmp_path)
    metrics = {m["name"]: m for m in yaml.safe_load(exported.text)["metrics"]}
    for offset, (window, _) in DBT_OFFSETS.items():
        assert metrics[f"paid_value_{offset.replace(' ', '_')}"]["type_params"] == {
            "expr": "paid_value",
            "metrics": [{"name": "paid_value", "offset_window": window}],
        }
    assert metrics["orders_per_buyer_1_month"]["type_params"] == {
        "expr": "orders_per_buyer * 100",
        "metrics": [{"name": "orders_per_buyer", "offset_window": "1 month"}],
    }
    shifted = [m.name for m in project.metrics if m.type == "derived" and m.inputs]
    assert len(shifted) == 25
    back_metrics = {m.name: m for m in back.project.metrics}
    for name in [*shifted, "revenue_yoy"]:
        assert back_metrics[name].inputs == full.metrics[name].inputs
    # One export warning per shifted metric, and one import warning per offset.
    assert sum("on `metric_time`" in w for w in exported.warnings) == len(shifted)
    assert (
        "metrics.paid_value_12_months: MetricFlow shifts `paid_value` by `12 months` "
        "(`offset_window: 1 year`) on `metric_time`, which is `payments.paid_at`; by that "
        "dimension at the grains the engine accepts (month, quarter, year) the periods"
    ) in "\n".join(exported.warnings)
    assert sum("earlier (`offset_window: " in w for w in back.warnings) == len(shifted)
    assert not any("clamps month ends" in w for w in back.warnings)
    other = compile_project(back.project)
    since = f"DATE '{(dt.date.today() - dt.timedelta(days=200)).isoformat()}'"
    refused = compared = 0
    for name in [*shifted, "revenue_yoy"]:
        payments = name.startswith(("paid_value", "payment_count"))
        model, time = ("payments", "paid_at") if payments else ("orders", "ordered_at")
        extra = "payments.method" if payments else "orders.status"
        for grain in GRAINS:
            for dims, filters in (
                ([f"{model}.{time}__{grain}"], []),
                ([extra, f"{model}.{time}__{grain}"], []),
                ([f"{model}.{time}__{grain}"], [f"{{{model}.{time}}} >= {since}"]),
            ):
                query = QueryRequest(metrics=[name], dimensions=dims, filters=filters)
                try:
                    native = rows(full, connector, query)
                except QueryError as refusal:
                    refused += 1
                    with pytest.raises(QueryError) as again:
                        rows(other, connector, query)
                    assert str(again.value) == str(refusal)
                    continue
                assert native
                same(native, rows(other, connector, query))
                compared += 1
    # `revenue_yoy` reads `revenue_12_months`, so it runs where that offset does.
    offsets = [full.metrics[n].inputs[0].offset for n in shifted] + ["12 months"]
    fits = [DBT_OFFSETS[offset][1] for offset in offsets]
    assert compared == 3 * sum(len(f) for f in fits)
    assert refused == 3 * sum(len(GRAINS) - len(f) for f in fits)


def test_dbt_export_offset_refusals(graph):
    """An offset over a model without a time dimension, or over a filtered count, is not
    exported; an offset over a ratio of two models names both `metric_time` dimensions."""
    data = _dbt_base(graph.project)
    data["metrics"] += [
        {
            "name": "products_prev",
            "type": "derived",
            "expr": "{product_count}",
            "inputs": [{"metric": "product_count", "offset": "1 month"}],
        },
        {
            "name": "completed_orders",
            "type": "simple",
            "measure": "order_count",
            "filter": "{status} = 'completed'",
        },
        {
            "name": "completed_prev",
            "type": "derived",
            "expr": "{completed_orders}",
            "inputs": [{"metric": "completed_orders", "offset": "7 days"}],
        },
        {
            "name": "paid_share",
            "type": "ratio",
            "numerator": "paid_value",
            "denominator": "revenue",
        },
        {
            "name": "paid_share_prev",
            "type": "derived",
            "expr": "{paid_share}",
            "inputs": [{"metric": "paid_share", "offset": "24 months"}],
        },
    ]
    result = export_dbt(Project.model_validate(data), allow_metric_loss=True)
    reasons = {w.split(": ", 1)[0]: w.split(": ", 1)[1] for w in result.warnings}
    assert reasons["metrics.products_prev"] == (
        "it reads `product_count` at an earlier period, and its model `products` has no time "
        "dimension for MetricFlow to shift; it is not exported to dbt"
    )
    assert reasons["metrics.completed_prev"] == (
        "it uses `completed_orders`, a filtered count, which MetricFlow returns as NULL where "
        "the engine returns 0; it is not exported to dbt"
    )
    assert reasons["metrics.paid_share_prev"].startswith(
        "MetricFlow shifts `paid_share` by `24 months` (`offset_window: 2 years`) on "
        "`metric_time`, which is `orders.ordered_at`, `payments.paid_at`; by those dimensions "
        "at the grains the engine accepts (month, quarter, year)"
    )
    exported = {m["name"] for m in yaml.safe_load(result.text)["metrics"]}
    assert "paid_share_prev" in exported
    assert not {"products_prev", "completed_prev"} & exported


def _dbt_input(name: str, window: str | None = None, **extra) -> dict:
    item = {"name": name, **extra}
    if window:
        item["offset_window"] = window
    return item


def _dbt_offset_doc(project: Project) -> dict:
    """The base model as dbt YAML, plus derived metrics with MetricFlow offsets: one per unit
    MetricFlow accepts, an aliased input read both shifted and unshifted, and what Cosmos
    cannot shift."""
    doc = yaml.safe_load(export_dbt(Project.model_validate(_dbt_base(project))).text)

    def derived(name: str, expr: str, *inputs: dict) -> dict:
        params = {"expr": expr, "metrics": list(inputs)}
        return {"name": name, "label": name, "type": "derived", "type_params": params}

    doc["metrics"] += [
        derived("rev_1_week", "revenue", _dbt_input("revenue", "1 week")),
        derived("rev_2_months", "revenue", _dbt_input("revenue", "2 Months")),
        derived("rev_3_months", "revenue", _dbt_input("revenue", "3 months")),
        derived("rev_1_quarter", "revenue", _dbt_input("revenue", "1 quarter")),
        derived("rev_1_year", "revenue", _dbt_input("revenue", "1 year")),
        derived("pay_1_day", "payment_count", _dbt_input("payment_count", "1 day")),
        derived(
            "rev_yoy_gap",
            "revenue - revenue_ly",
            _dbt_input("revenue"),
            _dbt_input("revenue", "1 year", alias="revenue_ly"),
        ),
        # Refused, each with a warning.
        derived("rev_to_grain", "revenue", _dbt_input("revenue", offset_to_grain="month")),
        derived("rev_hour", "revenue", _dbt_input("revenue", "1 hour")),
        derived("rev_zero", "revenue", _dbt_input("revenue", "0 days")),
        derived("rev_fiscal", "revenue", _dbt_input("revenue", "1 fiscal_quarter")),
        derived("rev_nested", "rev_1_year", _dbt_input("rev_1_year", "1 month")),
        derived(
            "rev_nested_gap",
            "revenue - rev_nested",
            _dbt_input("revenue"),
            _dbt_input("rev_nested"),
        ),
        {
            "name": "rev_7d",
            "label": "rev_7d",
            "type": "cumulative",
            "type_params": {"measure": "revenue", "cumulative_type_params": {"window": "7 days"}},
        },
        derived("rev_7d_prev", "rev_7d", _dbt_input("rev_7d", "1 month")),
    ]
    return doc


@pytest.mark.parametrize("source", SOURCES)
def test_dbt_import_offset_windows_same_numbers(graph, tmp_path, source, request):
    """`cosmos import dbt` reads `offset_window` in every unit MetricFlow accepts as the
    Cosmos offset in days or months, with the numbers of the same offset written in Cosmos;
    `offset_to_grain`, other units and offsets Cosmos cannot shift are skipped with a
    warning (DEC-36)."""
    connector = request.getfixturevalue(source)
    path = tmp_path / "dbt" / "semantic_models.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(_dbt_offset_doc(graph.project), sort_keys=False))
    result = import_model("dbt", path, schema="public")
    metrics = {m.name: m for m in result.project.metrics}
    expected = {
        "rev_1_week": "7 days",
        "rev_2_months": "2 months",
        "rev_3_months": "3 months",
        "rev_1_quarter": "3 months",
        "rev_1_year": "12 months",
        "pay_1_day": "1 day",
        "rev_yoy_gap__revenue_ly": "12 months",
    }
    for name, offset in expected.items():
        assert [i.model_dump() for i in metrics[name].inputs] == [
            {"metric": "payment_count" if name == "pay_1_day" else "revenue", "offset": offset}
        ]
    # The input read both ways becomes a metric of its own.
    assert metrics["rev_yoy_gap"].expr == "{revenue} - {rev_yoy_gap__revenue_ly}"
    assert metrics["rev_yoy_gap"].inputs == []
    skipped = {
        "rev_to_grain",
        "rev_hour",
        "rev_zero",
        "rev_fiscal",
        "rev_nested",
        "rev_nested_gap",
        "rev_7d_prev",
    }
    assert not skipped & set(metrics)
    warnings = {w.split(": ", 1)[0]: w.split(": ", 1)[1] for w in result.warnings}
    assert len(warnings) == len(result.warnings)  # one warning per metric, none for a skipped one
    assert warnings["metrics.rev_to_grain"] == (
        "input `revenue` uses `offset_to_grain: month`, which Cosmos does not compute; metric "
        "skipped"
    )
    for name, window in (
        ("rev_hour", "1 hour"),
        ("rev_zero", "0 days"),
        ("rev_fiscal", "1 fiscal_quarter"),
    ):
        assert warnings[f"metrics.{name}"] == (
            f"`offset_window: {window}` is not supported (only a whole number of days, weeks, "
            "months, quarters or years above zero); metric skipped"
        )
    assert warnings["metrics.rev_nested"] == (
        "`rev_1_year` is read with an offset, and it uses `rev_1_year`, which has offsets of "
        "its own, which Cosmos does not shift; metric skipped"
    )
    assert warnings["metrics.rev_7d_prev"] == (
        "`rev_7d` is read with an offset, and it uses the cumulative metric `rev_7d`, which "
        "Cosmos does not shift; metric skipped"
    )
    assert (
        warnings["metrics.rev_nested_gap"]
        == "depends on `rev_nested`, which was not imported; skipped"
    )
    assert warnings["metrics.rev_1_year"] == (
        "`revenue` is read `12 months` earlier (`offset_window: 1 year`), queried by month or "
        "quarter or year. Cosmos moves the start of the query's period, filters the facts "
        "before the shift and returns only periods with facts; MetricFlow joins a time spine, "
        "so it also returns periods without facts whose earlier period has some, and it "
        "applies filters on `metric_time` after the shift"
    )
    # A window in months queried by a coarser grain: MetricFlow shifts each day there.
    assert warnings["metrics.rev_3_months"].endswith(
        "; by quarter MetricFlow shifts each day before aggregating and clamps month ends, so "
        "its numbers differ there from Cosmos's, which shift the whole period"
    )
    assert "clamps" not in warnings["metrics.rev_1_quarter"]
    # The imported metrics give the numbers of the same offsets written in Cosmos.
    native = _dbt_offset_metrics(graph.project)
    full = compile_project(native)
    other = compile_project(result.project)
    pairs = [
        ("rev_1_week", "revenue_7_days", ("day", "week")),
        ("rev_3_months", "revenue_3_months", ("month", "quarter")),
        ("rev_1_quarter", "revenue_3_months", ("month", "quarter")),
        ("rev_1_year", "revenue_12_months", ("month", "quarter", "year")),
        ("pay_1_day", "payment_count_1_day", ("day",)),
    ]
    for mine, theirs, grains in pairs:
        model, time = ("payments", "paid_at") if mine == "pay_1_day" else ("orders", "ordered_at")
        for grain in grains:
            query = QueryRequest(metrics=[mine], dimensions=[f"{model}.{time}__{grain}"])
            want = rows(full, connector, query.model_copy(update={"metrics": [theirs]}))
            assert want
            same(want, rows(other, connector, query))
    query = QueryRequest(metrics=["rev_yoy_gap"], dimensions=["orders.ordered_at__month"])
    gap = rows(other, connector, query)
    by_month = {
        r[0]: r[1:]
        for r in rows(
            full,
            connector,
            QueryRequest(
                metrics=["revenue", "revenue_12_months"], dimensions=["orders.ordered_at__month"]
            ),
        )
    }
    assert len(gap) == len(by_month)
    for month, value in gap:
        revenue, earlier = by_month[month]
        assert (
            value is None
            if earlier is None
            else math.isclose(value, revenue - earlier, rel_tol=1e-6)
        )


def test_dbt_export_states_each_reason(graph):
    data = _without_policies(_without_business(graph.project))
    data["metrics"] += [
        {
            "name": "recent_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{orders.ordered_at__month} >= DATE '2026-01-01'",
        },
        {
            "name": "january_revenue",
            "type": "simple",
            "measure": "revenue",
            "filter": "{orders.ordered_at} <= '2026-01-31'",
        },
        {"name": "my_revenue", "type": "simple", "measure": "revenue", "filter": "$user.id = 1"},
        {"name": "raw_gap", "type": "derived", "expr": "{revenue} - amount"},
        {"name": "gap_share", "type": "ratio", "numerator": "raw_gap", "denominator": "revenue"},
        {
            "name": "ytd_share",
            "type": "ratio",
            "numerator": "revenue_year_to_date",
            "denominator": "revenue",
        },
        {"name": "revenue_7d", "type": "cumulative", "measure": "revenue", "window": "7 days"},
        # Filtered counts are exported, but not as inputs of ratio or derived metrics; a
        # filtered sum is.
        {
            "name": "completed_orders",
            "type": "simple",
            "measure": "order_count",
            "filter": "{status} = 'completed'",
        },
        {
            "name": "completed_buyers",
            "type": "simple",
            "measure": "buyers",
            "filter": "{status} = 'completed'",
        },
        {"name": "open_orders", "type": "derived", "expr": "{order_count} - {completed_orders}"},
        {
            "name": "completion_rate",
            "type": "ratio",
            "numerator": "completed_orders",
            "denominator": "order_count",
        },
        {
            "name": "buyer_share",
            "type": "ratio",
            "numerator": "completed_buyers",
            "denominator": "buyers",
        },
        {"name": "open_revenue", "type": "derived", "expr": "{revenue} - {completed_revenue}"},
        # A ratio's own filter reaches every input: refused over a count across models or
        # over a nested metric, exported for two plain inputs on one model.
        {
            "name": "pay_per_order_f",
            "type": "ratio",
            "numerator": "payment_count",
            "denominator": "order_count",
            "filter": "{orders.status} = 'completed'",
        },
        {"name": "cnt_diff", "type": "derived", "expr": "{order_count} - {payment_count}"},
        {
            "name": "cnt_diff_share",
            "type": "ratio",
            "numerator": "cnt_diff",
            "denominator": "revenue",
            "filter": "{orders.status} = 'completed'",
        },
        {
            "name": "completed_aov",
            "type": "ratio",
            "numerator": "revenue",
            "denominator": "order_count",
            "filter": "{status} = 'completed'",
        },
    ]
    # A rolling window is exported; one in another unit, over a model without a time
    # dimension or inside another metric is not, and neither is a running total without a
    # period. A running total since the start of a period is exported over a `sum` or `count`
    # measure (DEC-31); one over a `min` or `max` measure, or over a model without a time
    # dimension, is not.
    data["metrics"] += [
        {
            "name": "smallest_mtd",
            "type": "cumulative",
            "measure": "smallest_order",
            "grain_to_date": "month",
        },
        {
            "name": "products_mtd",
            "type": "cumulative",
            "measure": "product_count",
            "grain_to_date": "month",
        },
        {"name": "revenue_month", "type": "cumulative", "measure": "revenue", "window": "1 month"},
        {
            "name": "products_7d",
            "type": "cumulative",
            "measure": "product_count",
            "window": "7 days",
        },
        {"name": "revenue_running", "type": "cumulative", "measure": "revenue"},
        {
            "name": "rolling_share",
            "type": "ratio",
            "numerator": "revenue_rolling_7_days",
            "denominator": "revenue",
        },
        {"name": "rolling_gap", "type": "derived", "expr": "{revenue_7d} - {revenue}"},
    ]
    project = Project.model_validate(data)
    with pytest.raises(MetricLossError) as exc:
        export_dbt(project)
    assert str(exc.value).startswith(
        "19 metrics (max_order_value_year_to_date, recent_revenue, january_revenue, "
    )
    result = export_dbt(project, allow_metric_loss=True)
    reasons = {w.split(": ", 1)[0]: w.split(": ", 1)[1] for w in result.warnings}
    for name, agg in (("max_order_value_year_to_date", "max"), ("smallest_mtd", "min")):
        assert reasons[f"metrics.{name}"] == (
            f"it is a running total over a measure that aggregates with `{agg}`, and the dbt "
            "export carries running totals of `sum` and `count` measures only; it is not "
            "exported to dbt"
        )
    assert reasons["metrics.products_mtd"] == (
        "its measure's model `products` has no time dimension for MetricFlow to accumulate "
        "over; it is not exported to dbt"
    )
    for name in TO_DATE:
        assert reasons[f"metrics.{name}"].startswith("MetricFlow accumulates over `metric_time`")
    for name in (*ROLLING, "revenue_7d"):
        assert reasons[f"metrics.{name}"].startswith("MetricFlow windows over `metric_time`")
    assert reasons["metrics.revenue_running"] == (
        "it is a running total without a window, and the dbt export does not carry it yet; it "
        "is not exported to dbt"
    )
    assert reasons["metrics.revenue_month"] == (
        "its window `1 month` is not in days, the only unit the engine computes; it is not "
        "exported to dbt"
    )
    assert reasons["metrics.products_7d"] == (
        "its measure's model `products` has no time dimension for MetricFlow to window over; it "
        "is not exported to dbt"
    )
    for name, ref in (("rolling_share", "revenue_rolling_7_days"), ("rolling_gap", "revenue_7d")):
        assert reasons[f"metrics.{name}"] == (
            f"it uses the cumulative metric `{ref}`, and Cosmos does not compute a cumulative "
            "metric inside another one; it is not exported to dbt"
        )
    for name, ref in (
        ("recent_revenue", "orders.ordered_at__month"),
        ("january_revenue", "orders.ordered_at"),
    ):
        assert reasons[f"metrics.{name}"].startswith(
            f"its filter uses the time dimension `{ref}`, which MetricFlow compares truncated"
        )
    assert reasons["metrics.my_revenue"].startswith("its filter uses $user")
    assert reasons["metrics.raw_gap"].startswith("its expression uses bare columns (amount)")
    assert reasons["metrics.gap_share"].startswith("it uses `raw_gap`, which is not exported")
    assert reasons["metrics.ytd_share"].startswith(
        "it uses the cumulative metric `revenue_year_to_date`, and Cosmos does not compute"
    )
    for name, ref in (
        ("open_orders", "completed_orders"),
        ("completion_rate", "completed_orders"),
        ("buyer_share", "completed_buyers"),
    ):
        assert reasons[f"metrics.{name}"] == (
            f"it uses `{ref}`, a filtered count, which MetricFlow returns as NULL where the "
            "engine returns 0; it is not exported to dbt"
        )
    for name, count in (("pay_per_order_f", "order_count"), ("cnt_diff_share", "order_count")):
        assert reasons[f"metrics.{name}"] == (
            f"its filter turns the count `{count}` into a filtered count, which MetricFlow "
            "returns as NULL where the engine returns 0; it is not exported to dbt"
        )
    lost = {k.split(".", 1)[1] for k, r in reasons.items() if r.endswith("not exported to dbt")}
    assert len(lost) == 19
    exported = {m["name"] for m in yaml.safe_load(result.text)["metrics"]}
    assert {*ROLLING, *TO_DATE, "revenue_7d"} <= exported
    assert not exported & lost
    assert {"completed_orders", "completed_buyers", "open_revenue", "cnt_diff"} <= exported
    assert "completed_aov" in exported
    # A business rule in a filter is refused too.
    rules = graph.project.model_dump(mode="json")
    rules["metrics"].append(
        {
            "name": "active_customers",
            "type": "simple",
            "measure": "customer_count",
            "filter": "{rule.active_customer}",
        }
    )
    warnings = export_dbt(Project.model_validate(rules), **LOSS_FLAGS).warnings
    assert (
        "metrics.active_customers: its filter uses the business rule `rule.active_customer`, "
        "not exported yet; it is not exported to dbt"
    ) in warnings


def test_cli_export_dbt(tmp_path):
    runner = CliRunner()
    refused = runner.invoke(app, ["export", "dbt", "-m", str(EXAMPLE)])
    assert refused.exit_code == 1
    assert refused.stdout == ""
    assert "1 row filter (customers.by_region) and the sensitivity" in refused.stderr
    refused = runner.invoke(app, ["export", "dbt", "-m", str(EXAMPLE), "--allow-policy-loss"])
    assert refused.exit_code == 1
    assert "1 metric (max_order_value_year_to_date) would NOT be exported" in refused.stderr
    flags = ["--allow-policy-loss", "--allow-metric-loss"]
    result = runner.invoke(app, ["export", "dbt", "-m", str(EXAMPLE), *flags])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout)["semantic_models"]
    assert "warning:" not in result.stdout
    assert "warning: metrics.max_order_value_year_to_date: it is a running total" in result.stderr
    assert "warning: metrics.revenue_year_to_date: MetricFlow accumulates over" in result.stderr
    assert "warning: metrics.revenue_rolling_7_days: MetricFlow windows over" in result.stderr
    out = tmp_path / "models" / "semantic_models.yml"
    result = runner.invoke(app, ["export", "dbt", "-m", str(EXAMPLE), "-o", str(out), *flags])
    assert result.exit_code == 0, result.output
    assert "23 warnings" in result.stdout
    assert (
        len(import_model("dbt", out, schema="public").warnings) == len(ROLLING) + 3
    )  # and the three offsets
