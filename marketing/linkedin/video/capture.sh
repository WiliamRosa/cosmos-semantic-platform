#!/usr/bin/env bash
# Runs, for real, every command the video shows and saves each output to <work>/captured/.
# Needs Docker and uv. Starts the five databases from deploy/docker-compose.yml (project cosmos-semantic).
# Usage, from the repository root: bash marketing/linkedin/video/capture.sh <work dir>
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
WORK="$(mkdir -p "$1" && cd "$1" && pwd)"
OUT="$WORK/captured"
rm -rf "$OUT" "$WORK/env" && mkdir -p "$OUT" "$WORK/env"
export PYTHONIOENCODING=utf-8 COLUMNS=100 NO_COLOR=1 TERM=dumb

docker compose -p cosmos-semantic -f "$REPO/deploy/docker-compose.yml" \
  --profile sqlserver --profile mysql --profile mariadb --profile oracle up -d --wait
docker exec -i cosmos-semantic-oracle-1 sqlplus -s system/cosmos@FREEPDB1 < "$REPO/deploy/oracle-setup.sql" > /dev/null

# 1. pip install from PyPI in a brand-new virtual environment, as a reader would.
uv venv -q --seed --python 3.12 "$WORK/env/.venv"
PY="$WORK/env/.venv/bin/python"; [ -x "$PY" ] || PY="$WORK/env/.venv/Scripts/python"
"$PY" -m pip install --disable-pip-version-check "cosmos-semantic[duckdb,testdata,postgres,sqlserver,mysql,oracle]" > "$OUT/01-pip.txt" 2>&1
"$PY" -m pip install -q tzdata   # Windows has no system time zone database
C="$(dirname "$PY")/cosmos"

cd "$WORK/env"
"$C" init shop > "$OUT/02-init.txt" 2>&1
cd shop
"$C" demo-data > "$OUT/03-demo-data.txt" 2>&1
"$C" query revenue average_order_value --by orders.status --order orders.status --evidence > "$OUT/04-query-evidence.txt" 2>&1
docker ps --filter name=cosmos-semantic --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' > "$OUT/05-docker-ps.txt"
cat > connections.yml <<'YML'
default: ${COSMOS_DATA_SOURCE:-local}

data_sources:
  local:      { type: duckdb, path: data/ecommerce.duckdb }
  postgres:   { type: postgres, uri: "postgresql://cosmos:cosmos@localhost:15432/cosmos" }
  sqlserver:  { type: sqlserver, uri: "mssql+pymssql://sa:Cosmos!Passw0rd@localhost:14330/master", schema_map: { public: cosmos } }
  mysql:      { type: mysql, uri: "mysql+pymysql://root:cosmos@127.0.0.1:13306/" }
  mariadb:    { type: mariadb, uri: "mysql+pymysql://root:cosmos@127.0.0.1:13307/" }
  oracle:     { type: oracle, options: { dsn: "127.0.0.1:11521/FREEPDB1", user: cosmos, password: cosmos }, schema_map: { public: cosmos } }
YML
cp connections.yml "$OUT/06-connections.txt"
for db in postgres sqlserver mysql mariadb oracle; do "$C" demo-data --target "$db"; done > "$OUT/07-load.txt" 2>&1
for db in local postgres sqlserver mysql mariadb oracle; do
  echo "-- $db"
  COSMOS_DATA_SOURCE=$db "$C" query revenue --by orders.status --order orders.status
done > "$OUT/08-same-number.txt" 2>&1
"$C" query customer_count --by customers.email --agent > "$OUT/09-agent-pii.txt" 2>&1 || true
echo "captured into $OUT"
