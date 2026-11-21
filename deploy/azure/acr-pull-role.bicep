// Grants AcrPull on an existing registry to a principal. A separate module
// because the registry usually lives in a different resource group than the
// rest of main.bicep, and Bicep requires a module (its own deployment scope)
// for a resource targeted at a different resource group.

@description('Name of the existing registry (just the name, not the full resource ID).')
param registryName string

@description('Principal ID (e.g. a user-assigned managed identity) to grant AcrPull to.')
param principalId string

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, 'AcrPull')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
