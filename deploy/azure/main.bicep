// Resources deployed into the managed resource group of an Azure Marketplace
// Managed Application offer for Cosmos Semantic (ADR 0010). These resources
// live in the *customer's* subscription and are billed to them; the publisher
// (Wiliam Rosa) never runs or pays for this infrastructure.
//
// Compiles to mainTemplate.json with: az bicep build --file main.bicep
//
// First slice: a single Container App running the demo image
// (deploy/azure/Dockerfile), with Key Vault and a user-assigned managed
// identity wired up so a later connections.yml can pull real data-source
// secrets from Key Vault instead of plain environment variables.

@description('Location for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Short name used as a prefix for every resource name.')
@minLength(3)
@maxLength(20)
param namePrefix string = 'cosmossem'

@description('Container image reference. Fixed by the publisher; not exposed in the deployment wizard.')
param containerImage string = 'ghcr.io/wiliamrosa/cosmos-semantic-demo:latest'

@description('Microsoft Entra ID issuer URL for COSMOS_OIDC_ISSUER (e.g. https://login.microsoftonline.com/<tenant-id>/v2.0). Leave empty to deploy in anonymous/test mode.')
param oidcIssuer string = ''

@description('Microsoft Entra ID application (client) ID or API audience for COSMOS_OIDC_AUDIENCE. Leave empty to deploy in anonymous/test mode.')
param oidcAudience string = ''

var uniqueSuffix = uniqueString(resourceGroup().id)
var logAnalyticsName = '${namePrefix}-logs-${uniqueSuffix}'
var envName = '${namePrefix}-env-${uniqueSuffix}'
var appName = '${namePrefix}-app-${uniqueSuffix}'
var keyVaultName = take('${namePrefix}kv${uniqueSuffix}', 24)
var identityName = '${namePrefix}-identity-${uniqueSuffix}'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

// "Key Vault Secrets User" built-in role, scoped to this vault, granted to the
// Container App's identity so a future connections.yml can resolve
// ${KEY_VAULT_SECRET} values without the app ever holding vault-wide access.
resource keyVaultSecretsUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
      }
    }
    template: {
      containers: [
        {
          name: 'cosmos-semantic'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'COSMOS_OIDC_ISSUER'
              value: oidcIssuer
            }
            {
              name: 'COSMOS_OIDC_AUDIENCE'
              value: oidcAudience
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

@description('The public URL of the deployed Cosmos Semantic instance (REST API, MCP server and read-only console).')
output appUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'

@description('The console URL, for people who decide rather than build.')
output consoleUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}/console'
