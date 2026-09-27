// Phase 8 — Cardiology App hosting.
//
// One Container App serves the cardiology dashboard and its agent platform from
// the same origin. The model it calls, the registry it pulls from, and the
// identity that ties them together are declared here; Entra sign-in is applied
// by phase-8/deploy-cardiology-app.ps1 because it needs an app registration and
// a client secret, neither of which belongs in a template.
//
// Deployed twice per fresh environment: first with a public placeholder image
// (the registry is empty until the image is built), then with the built image.

@description('Short prefix for resource names.')
@maxLength(12)
param prefix string = 'cardioe2e'

@description('Region for every resource.')
param location string = resourceGroup().location

@description('Object ID of the deploying principal; granted model access for local runs.')
param principalId string

@description('Container image. The public placeholder is used until the registry holds a build.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('True once containerImage is in this deployment\'s registry.')
param useRegistryImage bool = false

@description('Build identifier reported by /api/health.')
param revision string = 'placeholder'

@description('Additional resource tags.')
param tags object = {}

@description('Entra client secret for Container Apps sign-in. A container app deployment replaces the app\'s whole secret set, so every deployment after sign-in is configured must pass it back or sign-in callbacks break.')
@secure()
param authClientSecret string = ''

var suffix = uniqueString(resourceGroup().id)
var allTags = union(tags, { 'hls-workload': 'cardiology-app', dataClassification: 'synthetic-only' })
var aiName = '${prefix}-ai-${suffix}'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  tags: allTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// Entra-only. The same name and properties as the app repo's own template, so a
// redeploy into a resource group that already has this account is a no-op.
resource ai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aiName
  location: location
  tags: allTags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: aiName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

// DataZoneStandard keeps inference in the US data zone. Capacity fits the
// eastus2 DataZoneStandard quota (333).
resource luna 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: ai
  name: 'gpt-5.6-luna'
  sku: {
    name: 'DataZoneStandard'
    capacity: 300
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-luna'
      version: '2026-07-09'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}acr${suffix}'
  location: location
  tags: allTags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

// User-assigned so its roles exist before the app's first revision pulls.
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-app-id'
  location: location
  tags: allTags
}

var acrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var openAiUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource appPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, appIdentity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: acrPull
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource appInference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ai.id, appIdentity.id, 'openai-user')
  scope: ai
  properties: {
    roleDefinitionId: openAiUser
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Same assignment name the app repo's template uses for the deployer, so an
// existing grant is reused rather than duplicated.
resource deployerInference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ai.id, principalId, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: ai
  properties: {
    roleDefinitionId: openAiUser
    principalId: principalId
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-cae'
  location: location
  tags: allTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-app'
  location: location
  tags: allTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${appIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        // The placeholder image listens on 80; the app on 4317.
        targetPort: useRegistryImage ? 4317 : 80
        transport: 'auto'
        allowInsecure: false
        // No corsPolicy: the dashboard is served from this origin.
      }
      registries: useRegistryImage ? [
        {
          server: acr.properties.loginServer
          identity: appIdentity.id
        }
      ] : []
      // Name fixed by `az containerapp auth microsoft update`.
      secrets: empty(authClientSecret) ? [] : [
        {
          name: 'microsoft-provider-authentication-secret'
          value: authClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'cardiology-app'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'CALDOVA_PROFILE', value: 'live' }
            { name: 'FOUNDRY_ENDPOINT', value: 'https://${aiName}.cognitiveservices.azure.com' }
            { name: 'FOUNDRY_DEPLOYMENT', value: luna.name }
            { name: 'CALDOVA_TICK_MS', value: '2000' }
            { name: 'CALDOVA_REVISION', value: revision }
            { name: 'AZURE_CLIENT_ID', value: appIdentity.properties.clientId }
          ]
          probes: useRegistryImage ? [
            {
              type: 'Readiness'
              httpGet: { path: '/api/health', port: 4317 }
              periodSeconds: 10
              failureThreshold: 6
            }
          ] : []
        }
      ]
      // EXACTLY ONE REPLICA: cohort, approvals, and runs live in process memory
      // (the Cosmos-backed profile is blocked), so a second replica would split state.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [appPull, appInference]
}

output appName string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output aiAccountName string = ai.name
output identityClientId string = appIdentity.properties.clientId
