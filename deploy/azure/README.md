# Azure Marketplace Managed Application (first slice)

See [ADR 0015](../../docs/decisions/0015-azure-marketplace-managed-application.md) for why this is a Managed Application offer, not a SaaS offer, and what the first slice does and does not cover.

Everything here was validated end-to-end in a real subscription on 2026-11-21 (see the dated update in ADR 0015): image build, ACR push, Bicep deploy, and the actual Marketplace-style wizard and overview chrome via a private Service Catalog Managed Application. What is **not** done, since it needs the maintainer's own accounts and explicit approval each time: publishing the image anywhere public, and any Partner Center step (account enrollment, offer listing, certification, pricing).

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

`main.bicep`'s `containerImage` parameter defaults to `ghcr.io/wiliamrosa/cosmos-semantic-demo:latest`, a public image: a published offer should use one, since the customer's subscription needs no registry credentials to pull it. GHCR is the simplest choice since it is tied to your existing GitHub account:

```bash
docker tag cosmos-semantic-demo:local ghcr.io/wiliamrosa/cosmos-semantic-demo:latest
echo "$GITHUB_TOKEN" | docker login ghcr.io -u WiliamRosa --password-stdin
docker push ghcr.io/wiliamrosa/cosmos-semantic-demo:latest
```

(Needs a GitHub token with `write:packages`.)

**Testing with a private registry instead** (e.g. your own Azure Container Registry, if you don't have that GitHub token scope handy): build remotely with `az acr build --registry <name> --image cosmos-semantic-demo:test -f deploy/azure/Dockerfile .`, no local push credentials needed. Then pass both `containerImage=<acr-login-server>/cosmos-semantic-demo:test` **and** `registryResourceId=<full ARM resource ID of the registry>` when deploying (step 4); the second parameter is what lets `main.bicep` grant `AcrPull` at the registry's own scope, in `acr-pull-role.bicep`. Expect the first deploy after creating a fresh identity to take a few extra minutes before the role takes effect; a container stuck failing on "unable to pull image using Managed identity" right after a first deploy is this, not a configuration error, retry the deployment once it's had a few minutes.

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

A plain `az deployment group create` does not show the wizard or the Managed Application overview/side-menu chrome, it is just a flat template deployment. To see that experience without a Partner Center offer, publish a **private Service Catalog Managed Application** in your own tenant instead (validated 2026-11-21):

```bash
az provider register --namespace Microsoft.Solutions   # once per subscription
az group create --name rg-cosmos-semantic-appdef --location eastus2

PRINCIPAL_ID=$(az ad signed-in-user show --query id -o tsv)
az managedapp definition create \
  --resource-group rg-cosmos-semantic-appdef \
  --name cosmos-semantic-demo \
  --location eastus2 \
  --display-name "Cosmos Semantic (demo)" \
  --description "Private test; not published anywhere." \
  --lock-level None \
  --authorizations "$PRINCIPAL_ID:8e3af657-a8ff-443c-a75c-2fe8c4bcb635" \
  --main-template @deploy/azure/mainTemplate.json \
  --create-ui-definition @deploy/azure/createUiDefinition.json
```

(`8e3af657-a8ff-443c-a75c-2fe8c4bcb635` is the built-in "Owner" role, granted here to yourself since you are both publisher and customer in this test.) Then in the Portal, open the definition's resource (**Home → rg-cosmos-semantic-appdef → cosmos-semantic-demo**) and use **Deploy from definition** to see the actual `createUiDefinition.json` wizard, then the resulting `Microsoft.Solutions/applications` resource's Overview blade and side menu. Nothing here is visible to anyone outside your own tenant; it never touches Partner Center.

Tear everything down when done:

```bash
az group delete --name cosmos-semantic-test --yes
az group delete --name rg-cosmos-semantic-appdef --yes
# plus the managed resource group an instance created, named after the instance (rg-cosmos-semantic-demo-<timestamp> or similar)
```

## 5. Package for Partner Center

A Managed Application package is a `.zip` of exactly these two files at its root:

```bash
cd deploy/azure
zip cosmos-semantic-managed-app.zip mainTemplate.json createUiDefinition.json
```

Uploading that zip to a Partner Center offer, choosing pricing, and submitting for certification are separate, explicit steps for you to take; nothing here does that automatically.
