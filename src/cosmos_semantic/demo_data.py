"""Synthetic data generator for a Brazilian e-commerce, for tests and demos.

Deterministic given the seed and the anchor date: orders cover the 18 months before the
anchor, so rules like "customer active in the last 90 days" always have cases on both sides.
Includes CPF (Brazilian taxpayer id), e-mail and health plan to exercise LGPD rules. Values
are in Portuguese on purpose: this is Brazilian data.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pyarrow as pa

REGIONS = {
    "Sul": ["RS", "SC", "PR"],
    "Sudeste": ["SP", "RJ", "MG", "ES"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Nordeste": ["BA", "PE", "CE", "MA", "PB", "RN", "AL", "SE", "PI"],
    "Norte": ["AM", "PA", "RO", "TO", "AC", "AP", "RR"],
}
REGION_WEIGHTS = {"Sul": 15, "Sudeste": 42, "Centro-Oeste": 8, "Nordeste": 27, "Norte": 8}
CATEGORIES = ["Eletrônicos", "Casa", "Moda", "Esporte", "Livros", "Beleza", "Brinquedos"]
STATUSES = ["completed"] * 8 + ["canceled", "pending"]
METHODS = ["pix", "cartao", "boleto"]

TABLES = ("customers", "sellers", "products", "orders", "order_items", "payments")


def _state_region(rng: random.Random) -> tuple[str, str]:
    region = rng.choices(list(REGION_WEIGHTS), weights=list(REGION_WEIGHTS.values()))[0]
    return rng.choice(REGIONS[region]), region


def generate(
    customers: int = 2_000, *, seed: int = 42, anchor: dt.date | None = None
) -> dict[str, pa.Table]:
    from faker import Faker

    anchor = anchor or dt.date.today()
    rng = random.Random(seed)
    fake = Faker("pt_BR")
    fake.seed_instance(seed)
    start = anchor - dt.timedelta(days=540)

    sellers = []
    for i in range(1, max(customers // 20, 5) + 1):
        state, region = _state_region(rng)
        sellers.append({"seller_id": i, "name": fake.company(), "state": state, "region": region})

    products = []
    for i in range(1, 201):
        products.append(
            {
                "product_id": i,
                "name": f"{fake.word().capitalize()} {fake.word()}",
                # ~5% without a category: exercises null dimensions
                "category": None if rng.random() < 0.05 else rng.choice(CATEGORIES),
                "list_price": round(rng.uniform(15, 2_500), 2),
            }
        )

    customer_rows = []
    for i in range(1, customers + 1):
        state, region = _state_region(rng)
        customer_rows.append(
            {
                "customer_id": i,
                "name": fake.name(),
                "cpf": fake.cpf(),
                "email": f"cliente{i}@{fake.free_email_domain()}",
                "state": state,
                "region": region,
                "segment": rng.choices(
                    ["Varejo", "PME", "Corporativo", None], weights=[68, 22, 7, 3]
                )[0],
                "health_plan": rng.choices(["Nenhum", "Básico", "Premium"], weights=[55, 30, 15])[
                    0
                ],
                "created_at": start + dt.timedelta(days=rng.randint(-365, 300)),
                "last_order_at": None,
            }
        )

    orders, items, payments = [], [], []
    item_id = payment_id = 0
    for order_id in range(1, customers * 3 + 1):
        customer = rng.choice(customer_rows)
        ordered_at = dt.datetime.combine(
            start + dt.timedelta(days=rng.randint(0, 539)),
            dt.time(rng.randint(7, 22), rng.randint(0, 59)),
        )
        status = rng.choice(STATUSES)
        gross = 0.0
        for _ in range(rng.choices([1, 2, 3, 4], weights=[50, 30, 15, 5])[0]):
            item_id += 1
            product = rng.choice(products)
            quantity = rng.choices([1, 2, 3], weights=[75, 20, 5])[0]
            unit_price = round(product["list_price"] * rng.uniform(0.85, 1.05), 2)
            gross += quantity * unit_price
            items.append(
                {
                    "order_item_id": item_id,
                    "order_id": order_id,
                    "product_id": product["product_id"],
                    "seller_id": rng.choice(sellers)["seller_id"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                }
            )
        amount = round(gross, 2)
        discount = round(amount * rng.choice([0, 0, 0, 0.05, 0.10]), 2)
        orders.append(
            {
                "order_id": order_id,
                "customer_id": customer["customer_id"],
                "ordered_at": ordered_at,
                "status": status,
                "amount": amount,
                "discount": discount,
            }
        )
        if status != "canceled":
            due = round(amount - discount, 2)
            parts = (
                [due]
                if rng.random() < 0.8
                else [round(due * 0.6, 2), round(due - round(due * 0.6, 2), 2)]
            )
            for value in parts:
                payment_id += 1
                method = rng.choice(METHODS)
                payments.append(
                    {
                        "payment_id": payment_id,
                        "order_id": order_id,
                        "method": method,
                        "installments": rng.choice([1, 2, 3, 6, 10]) if method == "cartao" else 1,
                        "value": value,
                        "paid_at": ordered_at + dt.timedelta(hours=rng.randint(0, 72)),
                    }
                )
        last = customer["last_order_at"]
        if last is None or ordered_at.date() > last:
            customer["last_order_at"] = ordered_at.date()

    schemas = {
        "customers": pa.schema(
            [
                ("customer_id", pa.int64()),
                ("name", pa.string()),
                ("cpf", pa.string()),
                ("email", pa.string()),
                ("state", pa.string()),
                ("region", pa.string()),
                ("segment", pa.string()),
                ("health_plan", pa.string()),
                ("created_at", pa.date32()),
                ("last_order_at", pa.date32()),
            ]
        ),
        "sellers": pa.schema(
            [
                ("seller_id", pa.int64()),
                ("name", pa.string()),
                ("state", pa.string()),
                ("region", pa.string()),
            ]
        ),
        "products": pa.schema(
            [
                ("product_id", pa.int64()),
                ("name", pa.string()),
                ("category", pa.string()),
                ("list_price", pa.float64()),
            ]
        ),
        "orders": pa.schema(
            [
                ("order_id", pa.int64()),
                ("customer_id", pa.int64()),
                ("ordered_at", pa.timestamp("us")),
                ("status", pa.string()),
                ("amount", pa.float64()),
                ("discount", pa.float64()),
            ]
        ),
        "order_items": pa.schema(
            [
                ("order_item_id", pa.int64()),
                ("order_id", pa.int64()),
                ("product_id", pa.int64()),
                ("seller_id", pa.int64()),
                ("quantity", pa.int64()),
                ("unit_price", pa.float64()),
            ]
        ),
        "payments": pa.schema(
            [
                ("payment_id", pa.int64()),
                ("order_id", pa.int64()),
                ("method", pa.string()),
                ("installments", pa.int64()),
                ("value", pa.float64()),
                ("paid_at", pa.timestamp("us")),
            ]
        ),
    }
    rows = {
        "customers": customer_rows,
        "sellers": sellers,
        "products": products,
        "orders": orders,
        "order_items": items,
        "payments": payments,
    }
    return {name: pa.Table.from_pylist(rows[name], schema=schemas[name]) for name in TABLES}


def load_duckdb(con, tables: dict[str, pa.Table], schema: str = "public") -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for name, table in tables.items():
        con.register("__cosmos_tmp", table)
        con.execute(f"CREATE OR REPLACE TABLE {schema}.{name} AS SELECT * FROM __cosmos_tmp")
        con.unregister("__cosmos_tmp")


def load_postgres(con, tables: dict[str, pa.Table], schema: str = "public") -> None:
    """``con`` is an ADBC Postgres connection."""
    with con.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for name, table in tables.items():
            cur.adbc_ingest(name, table, mode="replace", db_schema_name=schema)
    con.commit()


_ARROW_TO_SQL = {
    pa.int64(): "BIGINT",
    pa.string(): "VARCHAR(200)",
    pa.float64(): "DOUBLE",
    pa.date32(): "DATE",
    pa.timestamp("us"): "TIMESTAMP",
}


def load_sql(
    connector, tables: dict[str, pa.Table], schema: str = "public", chunk: int = 500
) -> None:
    """Generic INSERT-based load, translated by SQLGlot into the source dialect.

    Used for Databricks and SQL Server, which have no Arrow ingestion through the same path.
    """
    import sqlglot
    from sqlglot import exp

    dialect = connector.dialect

    def run(statement: str) -> None:
        for sql in sqlglot.transpile(statement, read="duckdb", write=dialect):
            connector.run_script(sql)

    if dialect == "tsql":
        connector.run_script(
            f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{schema}') "
            f"EXEC('CREATE SCHEMA {schema}')"
        )
    else:
        run(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for name, table in tables.items():
        run(f"DROP TABLE IF EXISTS {schema}.{name}")
        cols = ", ".join(f"{f.name} {_ARROW_TO_SQL[f.type]}" for f in table.schema)
        run(f"CREATE TABLE {schema}.{name} ({cols})")
        rows = table.to_pylist()
        for start in range(0, len(rows), chunk):
            values = [
                exp.Tuple(expressions=[_sql_literal(v) for v in row.values()])
                for row in rows[start : start + chunk]
            ]
            insert = exp.insert(exp.Values(expressions=values), f"{schema}.{name}")
            connector.run_script(insert.sql(dialect=dialect))


def _sql_literal(value):
    from sqlglot import exp

    if value is None:
        return exp.null()
    if isinstance(value, dt.datetime):
        return exp.cast(exp.Literal.string(value.isoformat(sep=" ")), "TIMESTAMP")
    if isinstance(value, dt.date):
        return exp.cast(exp.Literal.string(value.isoformat()), "DATE")
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def load(connector, tables: dict[str, pa.Table]) -> None:
    from cosmos_semantic.connectors import ADBCPostgresConnector, DuckDBConnector

    schema = connector.config.schema_map.get("public", "public")
    if isinstance(connector, DuckDBConnector):
        load_duckdb(connector.connection, tables, schema)
    elif isinstance(connector, ADBCPostgresConnector):
        load_postgres(connector.connection, tables, schema)
    else:
        load_sql(connector, tables, schema)


def write_parquet(tables: dict[str, pa.Table], directory: str | Path) -> list[Path]:
    import pyarrow.parquet as pq

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, table in tables.items():
        path = out / f"{name}.parquet"
        pq.write_table(table, path)
        paths.append(path)
    return paths
