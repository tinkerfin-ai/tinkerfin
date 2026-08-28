export interface AgentModelCatalogItem {
  modelId: string
  displayName: string
  reasoningEnabled: boolean
  runtimeProfile: 'deepagents-v2'
  isDefault: boolean
}

export interface AgentModelCatalog {
  items: AgentModelCatalogItem[]
  defaultModelId?: string | null
}
