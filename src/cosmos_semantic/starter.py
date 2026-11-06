"""`cosmos init`: a ready-to-run starter project for people who installed the package.

The example model ships inside the package (it is the retail pack's model), so a starter project
needs no clone of the repository: the model files, a `connections.yml` for a local DuckDB file,
and a pointer to the JSON Schema for editor completion.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from cosmos_semantic.errors import CosmosError
from cosmos_semantic.packs import PACKS_DIR

SCHEMA_URL = (
    "https://raw.githubusercontent.com/WiliamRosa/cosmos-semantic-platform/{ref}/schema/"
    "cosmos-semantic.v1.schema.json"
)

CONNECTIONS = """\
# Credentials come from environment variables; never write secrets in this file.
default: local

data_sources:
  local:
    type: duckdb
    path: ${COSMOS_DUCKDB_PATH:-data/ecommerce.duckdb}

  # postgres:
  #   type: postgres
  #   uri: ${COSMOS_PG_URI}
  #
  # databricks:
  #   type: databricks
  #   options:
  #     http_path: ${DATABRICKS_HTTP_PATH}
  #     profile: ${DATABRICKS_CONFIG_PROFILE:-DEFAULT}  # `databricks auth login --profile <name>`
  #     catalog: ${DATABRICKS_CATALOG:-cosmos_demo}
  #
  # snowflake:
  #   type: snowflake
  #   options:
  #     account: ${SNOWFLAKE_ACCOUNT}               # <org>-<account>
  #     user: ${SNOWFLAKE_USER}
  #     private_key_file: ${SNOWFLAKE_PRIVATE_KEY_FILE}  # key-pair login; never commit the key
  #     warehouse: ${SNOWFLAKE_WAREHOUSE}
  #     database: ${SNOWFLAKE_DATABASE:-COSMOS_DEMO}
  #
  # mysql:                                         # or type: mariadb
  #   type: mysql
  #   uri: ${COSMOS_MYSQL_URI}                     # mysql+pymysql://user:password@host:3306/
  #
  # oracle:
  #   type: oracle
  #   options:
  #     dsn: ${COSMOS_ORACLE_DSN}                  # host:1521/service
  #     user: ${COSMOS_ORACLE_USER}
  #     password: ${COSMOS_ORACLE_PASSWORD}
  #   schema_map: { public: ${COSMOS_ORACLE_USER} }  # in Oracle a schema is a user
"""


DEV_KEYS = """\
# Development API keys: each one stands for a user and their context (for row filters).
# Local use only (COSMOS_DEV_API_KEYS=dev-keys.yml); production uses an OIDC provider.
keys:
  dev-midwest:
    subject: analyst@example.com
    user: { region: Midwest }
"""


def _schema_ref() -> str:
    try:
        installed = version("cosmos-semantic")
    except PackageNotFoundError:
        return "main"
    return "main" if "dev" in installed else f"v{installed}"


def write_starter(target: Path, *, force: bool = False) -> list[Path]:
    """Writes `semantic/*.yml` and `connections.yml` under ``target``; refuses to overwrite."""
    sources = sorted((PACKS_DIR / "retail" / "semantic").glob("*.yml"))
    outputs = [target / "semantic" / s.name for s in sources] + [
        target / "connections.yml",
        target / "dev-keys.yml",
    ]
    existing = [p for p in outputs if p.exists()]
    if existing and not force:
        names = ", ".join(str(p) for p in existing)
        raise CosmosError(f"refusing to overwrite {names}; use --force")
    (target / "semantic").mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_URL.format(ref=_schema_ref())
    for source, output in zip(sources, outputs, strict=False):
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        if lines and lines[0].startswith("# yaml-language-server:"):
            lines[0] = f"# yaml-language-server: $schema={schema}\n"
        output.write_text("".join(lines), encoding="utf-8")
    (target / "connections.yml").write_text(CONNECTIONS, encoding="utf-8")
    (target / "dev-keys.yml").write_text(DEV_KEYS, encoding="utf-8")
    return outputs
