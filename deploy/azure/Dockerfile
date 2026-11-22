# Demo image for the Azure Marketplace Managed Application offer (ADR 0010).
# Bakes in the e-commerce example model and a DuckDB demo dataset, so the
# Managed Application deploys into a working `cosmos serve` with no external
# database. Production connections replace `connections.yml` with a customer's
# own data source; see deploy/azure/README.md.
#
# Builds from this repository checkout rather than the published PyPI package:
# the example model under examples/ecommerce follows the engine's current
# (sometimes unreleased) authoring format, so the two must stay in lockstep.
# Once a PyPI release matches the example model in use, this can switch to
# `pip install "cosmos-semantic[...]==X.Y.Z"` instead.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 cosmos
WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src

RUN pip install --no-cache-dir ".[server,duckdb,testdata]"

COPY examples/ecommerce/semantic ./semantic
COPY examples/ecommerce/connections.yml ./connections.yml
COPY deploy/azure/entrypoint.sh ./entrypoint.sh

RUN chmod +x ./entrypoint.sh \
    && cosmos demo-data -c connections.yml \
    && chown -R cosmos:cosmos /app

USER cosmos

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]
