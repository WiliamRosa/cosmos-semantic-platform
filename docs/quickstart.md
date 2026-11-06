# Quickstart

Five minutes from `pip install` to a governed number with its evidence. Everything runs locally on
DuckDB; nothing needs a server or an account.

## 1. Install and create a project

```bash
pip install "cosmos-semantic[duckdb,testdata]"
cosmos init demo
cd demo
cosmos demo-data
```

`cosmos init` writes the e-commerce example model (`semantic/`) and a `connections.yml` for a local
DuckDB file. `cosmos demo-data` fills `data/ecommerce.duckdb` with 2,000 customers, 6,000 orders,
their items and payments.

## 2. Ask for a metric

```bash
cosmos query revenue average_order_value --by orders.status
```

```text
orders__status  revenue        average_order_value
--------------  -------------  -------------------
pending         1,578,795.78   2,626.95
canceled        1,518,830.03   2,548.37
completed       12,562,542.20  2,615.56
```

Your numbers differ: the data is generated around today's date. Add `--show-sql` to see the SQL the
engine wrote, or `--evidence` to see how it got there:

```text
evidence  q_2eae279c2b6b494da485
  model   edb8d14a70cd1e35  engine 0.1.0
  as of   2026-11-04 (America/New_York)  source local (duckdb)
  metric  revenue = SUM(orders.amount)
  lineage orders
  sql     eb5ca2c843244937  result 451c5e52d6f85089 (3 rows)
```

To take the result to a spreadsheet or a script, add `--csv`. Standard output is then only the CSV: a header, then one line per row, with values at full precision, without thousands separators. The `query_id` line goes to standard error, so redirecting gives a clean file:

```bash
cosmos query revenue average_order_value --by orders.status --csv > revenue.csv
```

```text
orders__status,revenue,average_order_value
canceled,1518830.029999999,2548.3725335570452
pending,1578795.779999999,2626.948053244591
completed,12562542.200000022,2615.561565688116
```

`--csv` and `--json` cannot be combined.

## 3. Business rules and policies

The model defines `active_customer` as a business rule with an owner, a history and tests. Use it
as a filter; customers are row-filtered by the user's region:

```bash
cosmos query customer_count --where "{rule.active_customer}" --by customers.segment -u region=Midwest
cosmos explain rule:active_customer
```

Personal data never reaches AI agents. The same query from an agent, grouped by e-mail, is refused:

```bash
cosmos query customer_count --by customers.email --agent -u region=Midwest
# error: `customers.email` is personal data (LGPD) and never reaches AI agents, ...
```

## 4. Connect an AI agent

```bash
pip install "cosmos-semantic[duckdb,testdata,server]"
```

`cosmos init` also wrote `dev-keys.yml`: a development key, `dev-midwest`, that stands for a user
in the Midwest region, so row filters apply to the agent too. A client starts the server from its
own working directory, so give it absolute paths. For Claude Code, from the `demo` folder:

```bash
claude mcp add cosmos \
  --env COSMOS_DEV_API_KEYS="$PWD/dev-keys.yml" \
  --env COSMOS_API_KEY=dev-midwest \
  --env COSMOS_DUCKDB_PATH="$PWD/data/ecommerce.duckdb" \
  -- cosmos mcp -m "$PWD/semantic" -c "$PWD/connections.yml"
```

Any MCP client works the same way: command `cosmos`, arguments `mcp -m <semantic> -c
<connections.yml>`, and those three environment variables. The agent gets five tools:

| Tool | What it does |
| --- | --- |
| `search_concepts` | Finds glossary concepts by term or synonym, in English or Portuguese ("compradores ativos" finds `active_customer`) |
| `explain` | Definition, owner, logic and history of a metric, rule, dimension or concept |
| `list_members` | The metrics, dimensions and rules it can ask for |
| `query` | Runs a metrics request with the user's policies and returns the evidence envelope |
| `show_sql` | The SQL the engine would run, without running it |

Ask it "how many active buyers do we have?": it finds the rule through the synonym, queries
`customer_count` filtered by `{rule.active_customer}` for the Midwest, and cannot group by e-mail.
For a shared server, `cosmos serve` runs the REST API (`/v1`) and MCP over HTTP (`/mcp`) with an
OIDC provider instead of development keys.

## Next

- Point `connections.yml` at Postgres, SQL Server, MySQL, MariaDB, Oracle, Databricks or
  Snowflake (extras `postgres`, `sqlserver`, `mysql`, `oracle`, `databricks`, `snowflake`) and
  run the same model there.
- Draft a model of your own database with `cosmos bootstrap`, or import one with
  `cosmos import dbt|cube|ossie|powerbi`.
- Read the [semantic model reference](semantic-model.md) and the [architecture](architecture.md).
