"""Model bootstrap (ADR 0008): a draft semantic model from the database catalog, without AI.

The draft is a starting point for review, never a published model:

- **Keys:** declared primary keys, otherwise a column named ``id`` or ``<singular>_id``
  confirmed unique and non-null in the data.
- **Relationships:** a ``<name>_id`` column that matches another table's key becomes a foreign
  entity; its coverage (rows without a match) is measured and reported.
- **Columns:** text, boolean and date columns become dimensions; numeric columns become
  candidate ``sum`` measures; every table gets a row count.
- **LGPD:** column names that usually hold personal or sensitive data get a suggested
  ``sensitivity``, listed in the report for review.

Every inference goes into the report, so a person can confirm or fix it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.connectors import Connector
from cosmos_semantic.errors import CosmosError, QueryError
from cosmos_semantic.expressions import table_ref
from cosmos_semantic.spec import Project

PERSONAL = re.compile(
    r"(^|_)(cpf|cnpj|rg|ssn|tax_id|taxid|tax_number|nif|nino|email|e_mail|phone|telefone|celular|mobile|address|endereco|"
    r"cep|zip|birth|nascimento|passport|passaporte|full_name|nome_completo)($|_)"
)
SENSITIVE = re.compile(
    r"(^|_)(health|saude|plano_saude|health_plan|diagnosis|diagnostico|religion|religiao|"
    r"ethnicity|etnia|race|raca|biometric|biometria|sexual|political|politic|union|sindicato|"
    r"genetic|genetico)($|_)"
)
PERSON_TABLE = re.compile(
    r"(customer|client|user|employee|person|people|patient|member|contact|usuario|"
    r"funcionario|paciente|pessoa|cliente|aluno|student)"
)
PERSON_NAME = re.compile(r"(^|_)(name|nome|first_name|last_name|sobrenome)($|_)")
AVERAGE_LIKE = re.compile(r"(price|preco|rate|taxa|pct|percent|ratio|score|nota)")
LOW_CARDINALITY = 12
# Base type names as information_schema reports them on DuckDB, Postgres, SQL Server and
# Databricks (which says LONG, SHORT, BYTE and TIMESTAMP_NTZ).
INTEGER_TYPES = {
    "int",
    "integer",
    "bigint",
    "smallint",
    "tinyint",
    "hugeint",
    "long",
    "short",
    "byte",
}
NUMBER_TYPES = INTEGER_TYPES | {
    "numeric",
    "decimal",
    "double",
    "float",
    "real",
    "money",
    "smallmoney",
    "number",
}
TIME_TYPES = {
    "date",
    "time",
    "timestamp",
    "timestamptz",
    "datetime",
    "datetime2",
    "smalldatetime",
    "datetimeoffset",
}
BOOL_TYPES = {"bool", "boolean", "bit"}


def _base_type(data_type: str) -> str:
    """``BIGINT`` -> ``bigint``, ``DOUBLE PRECISION`` -> ``double``, ``TIMESTAMP_NTZ`` ->
    ``timestamp``, ``DECIMAL(10,2)`` -> ``decimal``, ``int4`` -> ``int``."""
    match = re.match(r"[a-z]+", data_type.strip().lower())
    return match.group() if match else ""


@dataclass
class Column:
    name: str
    data_type: str

    @property
    def kind(self) -> str:
        base = _base_type(self.data_type)
        if base in BOOL_TYPES:
            return "boolean"
        if base in NUMBER_TYPES:
            return "number"
        if base in TIME_TYPES:
            return "time"
        return "categorical"


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    declared_key: str | None = None


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _ident(name: str) -> str:
    out = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")
    return out if re.match(r"^[a-z_]", out) else f"t_{out}"


def _is_integer(data_type: str) -> bool:
    return _base_type(data_type) in INTEGER_TYPES


class Bootstrapper:
    def __init__(self, connector: Connector, schema: str = "public") -> None:
        self.connector = connector
        self.schema = schema
        self.physical_schema = connector.config.schema_map.get(schema, schema)
        self.report: dict[str, Any] = {
            "schema": schema,
            "tables": [],
            "keys": [],
            "relationships": [],
            "sensitivity": [],
            "warnings": [],
        }

    # -- catalog -----------------------------------------------------------------

    def _query(self, sql: str) -> list[dict[str, Any]]:
        return self.connector.execute(sql, max_rows=100_000).to_pylist()

    def _literal(self, value: str) -> str:
        return exp.Literal.string(value).sql(dialect=self.connector.dialect)

    def read_catalog(self, only: list[str] | None = None) -> list[Table]:
        schema = self._literal(self.physical_schema)
        rows = self._query(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema = {schema} ORDER BY table_name, ordinal_position"
        )
        tables: dict[str, Table] = {}
        for row in rows:
            values = list(row.values())
            name = str(values[0])
            if only and name not in only:
                continue
            tables.setdefault(name, Table(name)).columns.append(
                Column(str(values[1]), str(values[2]))
            )
        if not tables:
            raise QueryError(f"no tables found in schema `{self.physical_schema}`")
        try:
            keys = self._query(
                "SELECT k.table_name, k.column_name FROM information_schema.table_constraints c "
                "JOIN information_schema.key_column_usage k "
                "ON k.constraint_name = c.constraint_name AND k.table_schema = c.table_schema "
                f"AND k.table_name = c.table_name WHERE c.constraint_type = 'PRIMARY KEY' "
                f"AND c.table_schema = {schema}"
            )
        except CosmosError:
            keys = []
        per_table: dict[str, list[str]] = {}
        for row in keys:
            values = list(row.values())
            per_table.setdefault(str(values[0]), []).append(str(values[1]))
        for name, cols in per_table.items():
            if name in tables and len(cols) == 1:
                tables[name].declared_key = cols[0]
        return list(tables.values())

    # -- verification queries ----------------------------------------------------

    def _table(self, table: str, alias: str) -> exp.Table:
        return table_ref(f"{self.physical_schema}.{table}", self.connector.dialect).as_(alias)

    def _is_unique(self, table: str, column: str) -> bool:
        c = exp.column(column, table="t")
        sql = exp.select(
            exp.Count(this=exp.Star()).as_("n"),
            exp.Count(this=exp.Distinct(expressions=[c.copy()])).as_("d"),
            exp.Count(this=c.copy()).as_("nn"),
        ).from_(self._table(table, "t"))
        n, d, nn = list(self._query(sql.sql(dialect=self.connector.dialect))[0].values())
        return n > 0 and n == d == nn

    def _distinct(self, table: str, column: str) -> int:
        c = exp.column(column, table="t")
        sql = exp.select(exp.Count(this=exp.Distinct(expressions=[c])).as_("d")).from_(
            self._table(table, "t")
        )
        return int(list(self._query(sql.sql(dialect=self.connector.dialect))[0].values())[0])

    def _coverage(self, table: str, column: str, target: str, key: str) -> dict[str, int]:
        child = exp.column(column, table="c")
        parent = exp.column(key, table="p")
        orphan = exp.Case(
            ifs=[
                exp.If(
                    this=exp.and_(
                        exp.Is(this=parent.copy(), expression=exp.null()),
                        exp.Not(this=exp.Is(this=child.copy(), expression=exp.null())),
                    ),
                    true=exp.Literal.number(1),
                )
            ],
            default=exp.Literal.number(0),
        )
        sql = (
            exp.select(
                exp.Count(this=exp.Star()).as_("n"),
                exp.Count(this=child.copy()).as_("nn"),
                exp.Sum(this=orphan).as_("orphans"),
            )
            .from_(self._table(table, "c"))
            .join(
                self._table(target, "p"),
                on=exp.EQ(this=child.copy(), expression=parent.copy()),
                join_type="left",
            )
        )
        n, nn, orphans = list(self._query(sql.sql(dialect=self.connector.dialect))[0].values())
        return {"rows": int(n), "non_null": int(nn or 0), "orphans": int(orphans or 0)}

    # -- building the draft ------------------------------------------------------

    def build(self, only: list[str] | None = None) -> tuple[Project, dict[str, Any]]:
        started = time.perf_counter()
        tables = self.read_catalog(only)
        keys: dict[str, tuple[str, str]] = {}  # table -> (entity, column)
        for table in tables:
            entity = _ident(_singular(table.name))
            candidates = (
                [table.declared_key]
                if table.declared_key
                else [c.name for c in table.columns if c.name.lower() in ("id", f"{entity}_id")]
            )
            for column in candidates:
                declared = column == table.declared_key
                if declared or self._is_unique(table.name, column):
                    keys[table.name] = (entity, column)
                    self.report["keys"].append(
                        {
                            "table": table.name,
                            "column": column,
                            "entity": entity,
                            "source": "declared" if declared else "inferred (unique, not null)",
                        }
                    )
                    break
            else:
                self.report["warnings"].append(
                    f"{table.name}: no primary key found; other tables cannot join to it"
                )

        by_column = {column.lower(): (t, entity) for t, (entity, column) in keys.items()}
        models = []
        measure_names: dict[str, int] = {}
        for table in tables:
            model: dict[str, Any] = {
                "name": _ident(table.name),
                "table": f"{self.schema}.{table.name}",
                "description": f"Draft from {self.schema}.{table.name}; review before publishing.",
                "entities": [],
                "dimensions": [],
                "measures": [],
            }
            key_column = keys.get(table.name, (None, None))[1]
            if key_column:
                entity, column = keys[table.name]
                model["entities"].append(_entity(entity, "primary", column))
            for column in table.columns:
                lowered = column.name.lower()
                if column.name == key_column:
                    continue
                target = by_column.get(lowered)
                if target and target[0] != table.name and lowered.endswith("_id"):
                    target_table, entity = target
                    coverage = self._coverage(
                        table.name, column.name, target_table, keys[target_table][1]
                    )
                    model["entities"].append(_entity(entity, "foreign", column.name))
                    self.report["relationships"].append(
                        {
                            "from": f"{table.name}.{column.name}",
                            "to": f"{target_table}.{keys[target_table][1]}",
                            "entity": entity,
                            **coverage,
                        }
                    )
                    if coverage["orphans"]:
                        self.report["warnings"].append(
                            f"{table.name}.{column.name}: {coverage['orphans']} rows have no "
                            f"match in {target_table}; an INNER JOIN would drop them"
                        )
                    continue
                # Other ids are technical, except personal identifiers such as tax_id.
                if (lowered.endswith("_id") or lowered == "id") and not PERSONAL.search(lowered):
                    continue
                name = _ident(column.name)
                if column.kind == "number":
                    measure = name if name not in measure_names else f"{_ident(table.name)}_{name}"
                    measure_names[measure] = 1
                    agg = "avg" if AVERAGE_LIKE.search(lowered) else "sum"
                    model["measures"].append(
                        {
                            "name": measure,
                            "agg": agg,
                            **({"expr": column.name} if measure != column.name else {}),
                        }
                    )
                    if _is_integer(column.data_type) and (
                        self._distinct(table.name, column.name) <= LOW_CARDINALITY
                    ):
                        dim = {"name": name, "type": "number"}
                        if name != column.name:
                            dim["expr"] = column.name
                        model["dimensions"].append(dim)
                        self.report["warnings"].append(
                            f"{table.name}.{column.name}: few distinct values; kept as both a "
                            "dimension and a measure, keep the one that makes sense"
                        )
                    continue
                dim: dict[str, Any] = {"name": name, "type": column.kind}
                if name != column.name:
                    dim["expr"] = column.name
                level = _sensitivity(lowered, table.name.lower())
                if level:
                    dim["sensitivity"] = level
                    self.report["sensitivity"].append(
                        {"column": f"{table.name}.{column.name}", "suggested": level}
                    )
                model["dimensions"].append(dim)
            count = f"{_ident(table.name)}_count"
            model["measures"].append({"name": count, "agg": "count"})
            models.append({k: v for k, v in model.items() if v != []})
            self.report["tables"].append(
                {"table": table.name, "columns": len(table.columns), "model": model["name"]}
            )

        project = Project.model_validate({"version": 1, "semantic_models": models})
        graph = compile_project(project, strict=False)
        self.report["issues"] = [str(i) for i in graph.issues]
        self.report["seconds"] = round(time.perf_counter() - started, 2)
        return project, self.report


def _entity(name: str, kind: str, column: str) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "type": kind}
    if column != name:
        item["expr"] = column
    return item


def _sensitivity(column: str, table: str = "") -> str | None:
    if SENSITIVE.search(column):
        return "sensitive"
    if PERSONAL.search(column):
        return "personal"
    if PERSON_NAME.search(column) and PERSON_TABLE.search(table):
        return "personal"
    return None


def bootstrap(
    connector: Connector, schema: str = "public", only: list[str] | None = None
) -> tuple[Project, dict[str, Any]]:
    return Bootstrapper(connector, schema).build(only)
