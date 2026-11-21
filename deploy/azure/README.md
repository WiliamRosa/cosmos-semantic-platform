# Azure Marketplace Managed Application (first slice)

See [ADR 0015](../../docs/decisions/0015-azure-marketplace-managed-application.md) for why this is a Managed Application offer, not a SaaS offer, and what the first slice does and does not cover.

Everything here was validated locally (build, run, Bicep compile) before being committed. What was **not** done, since it needs the maintainer's own accounts and explicit approval each time: pushing the image to a registry, creating real Azure resources, and any Partner Center step.

## 1. Build the image

From the repository root:

```bash
docker build -f deploy/azure/Dockerfile -t cosmos-semantic-demo:local .
```

Smoke test locally:

```bash
docker run -d --name cosmos-demo -p 8080:8080 cosmos-semantic-demo:local
curl http://localhost:8080/healthz
curl -X POST http://localhost:8080/v1/query -H "Content-Type: application/json" -d '{"metrics":["revenue"]}'
# Console at http://localhost:8080/console
docker rm -f cosmos-demo
```

## 2. Push to a registry you control

`main.bicep`'s `containerImage` parameter defaults to `ghcr.io/wiliamrosa/cosmos-semantic-demo:latest`. GHCR is the simplest choice since it is tied to your existing GitHub account and is free for a public image:

```bash
docker tag cosmos-semantic-demo:local ghcr.io/wiliamrosa/cosmos-semantic-demo:latest
echo "$GITHUB_TOKEN" | docker login ghcr.io -u WiliamRosa --password-stdin
docker push ghcr.io/wiliamrosa/cosmos-semantic-demo:latest
```

(Needs a GitHub token with `write:packages`. If you'd rather use Azure Container Registry or Docker Hub instead, change `containerImage`'s default in `main.bicep` to match.)

## 3. Compile the Bicep template

```bash
az bicep build --file deploy/azure/main.bicep --outfile deploy/azure/mainTemplate.json
```

`mainTemplate.json` is git-ignored; regenerate it whenever `main.bicep` changes. Needed both for a direct `az deployment group create` test and for the Partner Center package in step 5.

## 4. Test-deploy it in your own subscription

This is the step that actually spends Azure credit (a small amount: one scale-to-zero Container App, a Key Vault, a Log Analytics workspace). Review `main.bicep`'s resource list before running this.

```bash
az group create --name cosmos-semantic-test --location eastus2
az deployment group create \
  --resource-group cosmos-semantic-test \
  --template-file deploy/azure/mainTemplate.json \
  --parameters containerImage=ghcr.io/wiliamrosa/cosmos-semantic-demo:latest
```

The deployment outputs `appUrl` and `consoleUrl`. Tear it down when done:

```bash
az group delete --name cosmos-semantic-test --yes
```

To see the wizard and the resulting Portal overview/side-menu experience (not just the CLI), use the Portal instead: **Deploy a custom template** → **Build your own template in the editor** for `mainTemplate.json`, or, once you have a Partner Center sandbox offer, the real "Create" wizard rendered from `createUiDefinition.json`.

## 5. Package for Partner Center

A Managed Application package is a `.zip` of exactly these two files at its root:

```bash
cd deploy/azure
zip cosmos-semantic-managed-app.zip mainTemplate.json createUiDefinition.json
```

Uploading that zip to a Partner Center offer, choosing pricing, and submitting for certification are separate, explicit steps for you to take; nothing here does that automatically.
