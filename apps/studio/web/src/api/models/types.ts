export interface AgentModelCatalogItem {
  modelId: string
  displayName: string
  reasoningEnabled: boolean
  isDefault: boolean
}

export interface AgentModelCatalog {
  items: AgentModelCatalogItem[]
  defaultModelId?: string | null
}
