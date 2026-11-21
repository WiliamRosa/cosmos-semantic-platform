# BigQuery test project

A free BigQuery sandbox project is enough to run the Cosmos test suite against BigQuery ([ADR 0015](../docs/decisions/0015-bigquery-as-a-data-source.md)). The sandbox needs no billing account. Its limits are 10 GB of storage and 1 TB of queries a month, and tables expire after 60 days. The test data takes a few megabytes.

## 1. Create the project

1. Open the [BigQuery console](https://console.cloud.google.com/bigquery) with a Google account and create a project, such as `cosmos-semantic-test`. Without a billing account, it is a sandbox.
2. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) and log in:

   ```bash
   gcloud auth login
   gcloud config set project cosmos-semantic-test
   ```

## 2. Choose how the tests log in

**On your own machine (simplest).** Your user credentials, which the client finds without any file:

```bash
gcloud auth application-default login
```

**For a service or CI.** A service account with the least privilege the tests need:

```bash
P=cosmos-semantic-test
bq --location=US mk --dataset $P:cosmos_demo
gcloud iam service-accounts create cosmos-tests --project $P
SA=cosmos-tests@$P.iam.gserviceaccount.com
# Run query jobs in the project:
gcloud projects add-iam-policy-binding $P --member serviceAccount:$SA --role roles/bigquery.jobUser
# Read and write only the test dataset:
bq add-iam-policy-binding --member serviceAccount:$SA --role roles/bigquery.dataEditor $P:cosmos_demo
gcloud iam service-accounts keys create ~/.config/cosmos/bigquery-tests.json --iam-account $SA
```

Keep the key outside the repository. For a read-only Cosmos source in production, grant `roles/bigquery.dataViewer` on the dataset instead of `dataEditor`.

## 3. Run the tests

```bash
export COSMOS_BIGQUERY_PROJECT=cosmos-semantic-test
export COSMOS_BIGQUERY_LOCATION=US
export COSMOS_BIGQUERY_DATASET=cosmos_demo
# only with a service account:
export COSMOS_BIGQUERY_CREDENTIALS_FILE=~/.config/cosmos/bigquery-tests.json
uv run --all-extras pytest -m bigquery
```

The first test loads the demo data with free load jobs, replacing the tables in `cosmos_demo`.

## Local emulator

Without a project, the [BigQuery emulator](https://github.com/goccy/bigquery-emulator) checks that the SQL passes BigQuery's analyzer and returns the same numbers as DuckDB:

```bash
docker compose -p cosmos-semantic -f deploy/docker-compose.yml --profile bigquery up -d
export COSMOS_BIGQUERY_PROJECT=cosmos-test COSMOS_BIGQUERY_ENDPOINT=http://localhost:19050 COSMOS_BIGQUERY_DATASET=cosmos
uv run --all-extras pytest -m bigquery -k "not cross_db and not reference"
```

The emulator is not BigQuery:

- It runs rolling windows for many minutes.
- It fails some hand-written reference queries with an internal error.

The live run is the gate.
