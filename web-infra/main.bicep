targetScope = 'resourceGroup'

param name string = 'promotion-policy-optimizer'
param location string = 'eastus2'

resource site 'Microsoft.Web/staticSites@2024-11-01' = {
  name: name
  location: location
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    buildProperties: {
      appLocation: 'web'
      apiLocation: 'api'
      appArtifactLocation: ''
    }
  }
}

output hostname string = site.properties.defaultHostname
output resourceId string = site.id
