// masimo-fhir-aggregator.bicep
// Azure Container Apps Job that writes 5-minute Masimo telemetry aggregates
// (Eventhouse TelemetryRaw) to FHIR as Observations, so each FHIR $export
// carries them into HDS. See masimo-fhir-aggregator/README.md.
//
// The job runs as a pre-provisioned user-assigned identity. Its grants are made
// by phase-2/deploy-masimo-fhir-aggregator.ps1 before this template is deployed:
//   - AcrPull on the container registry (image pull via the identity, no admin credentials)
//   - FHIR Data Contributor on the FHIR service
//   - Kusto database viewer on the Eventhouse KQL database

param location string = resourceGroup().location

@description('Existing Container Apps environment the job runs in.')
param environmentName string

@description('Container registry (same resource group) holding the image.')
param acrName string

@description('Full image reference, e.g. myacr.azurecr.io/masimo-fhir-aggregator:abc1234.')
param imageName string

@description('Resource ID of the user-assigned identity the job runs as.')
param identityId string

@description('Client ID of that identity (AZURE_CLIENT_ID for token requests).')
param identityClientId string

@description('FHIR service URL (also the token audience).')
param fhirServiceUrl string

@description('Eventhouse query URI (also the token audience).')
param kustoQueryUri string

@description('KQL database holding TelemetryRaw.')
param kustoDatabase string

param jobName string = 'masimo-fhir-aggregator'
param cronExpression string = '*/5 * * * *'
param windowMinutes string = '5'
param lookbackWindows string = '3'
param resourceTags object = {}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  tags: union(resourceTags, { 'hls-workload': 'masimo-fhir-aggregator', dataClassification: 'synthetic-only' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      // Under one schedule interval, so runs never overlap.
      replicaTimeout: 240
      replicaRetryLimit: 1
      registries: [
        {
          server: acr.properties.loginServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'masimo-fhir-aggregator'
          image: imageName
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'FHIR_URL', value: fhirServiceUrl }
            { name: 'KUSTO_QUERY_URI', value: kustoQueryUri }
            { name: 'KUSTO_DATABASE', value: kustoDatabase }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
            { name: 'WINDOW_MINUTES', value: windowMinutes }
            { name: 'LOOKBACK_WINDOWS', value: lookbackWindows }
          ]
        }
      ]
    }
  }
}

output jobName string = job.name
output jobId string = job.id
