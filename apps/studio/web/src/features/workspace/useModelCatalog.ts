import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchModelCatalog } from '../../api/models/client'
import type { AgentModelCatalogItem } from '../../api/models/types'

export function useModelCatalog() {
  const [models, setModels] = useState<AgentModelCatalogItem[]>([])
  const [catalogDefaultModelId, setCatalogDefaultModelId] = useState('')
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [version, setVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setStatus('loading')
    void fetchModelCatalog(controller.signal).then((catalog) => {
      if (controller.signal.aborted) return
      setModels(catalog.items)
      setCatalogDefaultModelId(catalog.defaultModelId ?? '')
      setStatus('ready')
    }).catch(() => {
      if (!controller.signal.aborted) setStatus('error')
    })
    return () => controller.abort()
  }, [version])

  const modelIds = useMemo(() => models.map((model) => model.modelId), [models])
  const defaultModelId = useMemo(() => (
    models.some((model) => model.modelId === catalogDefaultModelId)
      ? catalogDefaultModelId
      : models.find((model) => model.isDefault)?.modelId ?? models[0]?.modelId ?? ''
  ), [catalogDefaultModelId, models])
  const displayName = useCallback((modelId: string) => (
    models.find((model) => model.modelId === modelId)?.displayName ?? modelId
  ), [models])
  const retry = useCallback(() => setVersion((current) => current + 1), [])

  return {
    status,
    modelIds,
    defaultModelId,
    displayName,
    retry,
  }
}
