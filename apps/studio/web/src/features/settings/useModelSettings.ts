import { useEffect, useRef, useState } from 'react'
import { requestJson } from '../../api/shared/http'
import type { JsonObject } from '../../types'
import { splitModelOptions } from './modelOptions'

export interface ModelSettings {
  model_id: string
  display_name: string
  purpose: 'chat' | 'image'
  provider: 'deepseek' | 'openai'
  model_name: string
  base_url: string
  has_key: boolean
  image_support: 'supported' | 'unsupported' | 'unknown'
  reasoning_enabled: boolean
  enabled: boolean
  is_default: boolean
  sort_order: number
  generation_options: JsonObject
}

export const newModel = (): ModelSettings => ({
  model_id: crypto.randomUUID(),
  display_name: '',
  purpose: 'chat',
  provider: 'openai',
  model_name: '',
  base_url: 'https://api.openai.com/v1',
  has_key: false,
  image_support: 'unknown',
  reasoning_enabled: false,
  enabled: true,
  is_default: false,
  sort_order: 0,
  generation_options: {},
})

function normalizedEndpoint(value: string): string | null {
  try {
    return new URL(value).href.replace(/\/+$/, '')
  } catch {
    return null
  }
}

/** 管理本人模型配置，取消过期读取并串行提交，失败时保留编辑内容 */
export function useModelSettings(onChanged?: () => void) {
  const [models, setModels] = useState<ModelSettings[]>([])
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [revision, setRevision] = useState(0)
  const [editing, setEditing] = useState<ModelSettings>()
  const [key, setKey] = useState('')
  const [options, setOptions] = useState('{}')
  const [imageSize, setImageSize] = useState('')
  const [imageFormat, setImageFormat] = useState('')
  const [saving, setSaving] = useState(false)
  const savedModel = models.find((model) => model.model_id === editing?.model_id)
  const draftEndpoint = editing ? normalizedEndpoint(editing.base_url) : null
  const canReuseKey = Boolean(savedModel?.has_key && draftEndpoint
    && normalizedEndpoint(savedModel.base_url) === draftEndpoint)
  const [deleting, setDeleting] = useState<string>()
  const activeRequest = useRef<AbortController | null>(null)
  const loadRequest = useRef<AbortController | null>(null)
  useEffect(() => () => activeRequest.current?.abort(), [])
  useEffect(() => {
    const controller = new AbortController()
    loadRequest.current = controller
    setLoading(true)
    setLoadFailed(false)
    void requestJson<ModelSettings[]>('/api/models/configurations', {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setModels(value)
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadFailed(true)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [revision])
  const edit = (model: ModelSettings) => {
    setDeleting(undefined)
    setEditing(model)
    setKey('')
    const split = splitModelOptions(model.generation_options)
    setOptions(split.advanced)
    setImageSize(split.size)
    setImageFormat(split.format)
  }
  const cancelEdit = () => {
    if (activeRequest.current) return
    setEditing(undefined)
    setKey('')
    setOptions('{}')
    setImageSize('')
    setImageFormat('')
  }
  const save = async (generationOptions: JsonObject) => {
    if (!editing || activeRequest.current) return
    const controller = new AbortController()
    loadRequest.current?.abort()
    activeRequest.current = controller
    setSaving(true)
    try {
      const model = { ...editing, has_key: undefined }
      await requestJson<null>(
        `/api/models/configurations/${encodeURIComponent(editing.model_id)}`,
        {
          method: 'PUT',
          body: { ...model, api_key: key, generation_options: generationOptions },
          signal: controller.signal,
        },
      )
      if (controller.signal.aborted) return
      setEditing(undefined)
      setKey('')
      setRevision((value) => value + 1)
      onChanged?.()
    } catch {
      // 请求异常由全局提示展示，保留当前草稿或操作入口以便重试
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null
      if (!controller.signal.aborted) setSaving(false)
    }
  }
  const remove = async (modelId: string) => {
    if (activeRequest.current) return
    const controller = new AbortController()
    loadRequest.current?.abort()
    activeRequest.current = controller
    setSaving(true)
    try {
      await requestJson<null>(
        `/api/models/configurations/${encodeURIComponent(modelId)}`,
        {
          method: 'DELETE',
          signal: controller.signal,
        },
      )
      if (controller.signal.aborted) return
      setDeleting(undefined)
      setRevision((value) => value + 1)
      onChanged?.()
    } catch {
      // 请求异常由全局提示展示，保留当前草稿或操作入口以便重试
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null
      if (!controller.signal.aborted) setSaving(false)
    }
  }
  const makeDefault = async (modelId: string) => {
    if (activeRequest.current) return
    const controller = new AbortController()
    loadRequest.current?.abort()
    activeRequest.current = controller
    setSaving(true)
    try {
      await requestJson<null>(`/api/models/configurations/${encodeURIComponent(modelId)}/default`, {
        method: 'PUT', signal: controller.signal,
      })
      if (controller.signal.aborted) return
      setRevision((value) => value + 1)
      onChanged?.()
    } catch {
      // 请求异常由全局提示展示，保留当前草稿或操作入口以便重试
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null
      if (!controller.signal.aborted) setSaving(false)
    }
  }
  return { models, loading, loadFailed, editing, key, options, saving, deleting,
    setRevision, setEditing, setKey, setOptions, setDeleting, edit, cancelEdit, save, remove, makeDefault,
    imageSize, setImageSize, imageFormat, setImageFormat, canReuseKey }
}
