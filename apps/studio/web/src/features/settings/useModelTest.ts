import { useEffect, useRef, useState } from 'react'
import { requestJson } from '../../api/shared/http'
import type { ModelSettings } from './useModelSettings'

export type ModelTestKind = 'basic' | 'text' | 'vision' | 'image'
export type ModelTestConfiguration = Omit<ModelSettings, 'has_key'> & { api_key: string }
export interface ModelTestResult {
  kind: ModelTestKind
  outcome: 'success' | 'failed' | 'inconclusive'
  elapsed_ms: number
  code: string
  text: string | null
  image: { mime_type: string; data_base64: string } | null
}

/** 拥有当前草稿的一次测试，取消或编辑后拒绝旧响应，结果不持久化 */
export function useModelTest() {
  const active = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const [running, setRunning] = useState<ModelTestKind>()
  const [result, setResult] = useState<ModelTestResult>()
  const [error, setError] = useState<string>()
  const [stale, setStale] = useState(false)
  const [cancelled, setCancelled] = useState(false)
  useEffect(() => () => { generation.current += 1; active.current?.abort(); active.current = null }, [])
  const invalidate = () => {
    generation.current += 1
    active.current?.abort()
    active.current = null
    setRunning(undefined)
    setStale(true)
    setError(undefined)
    setCancelled(false)
  }
  const reset = () => { invalidate(); setResult(undefined); setStale(false) }
  const cancel = () => { invalidate(); setCancelled(true); setResult(undefined) }
  const run = async (kind: ModelTestKind, configuration: ModelTestConfiguration) => {
    if (active.current) return
    const sequence = ++generation.current
    const controller = new AbortController()
    active.current = controller
    setRunning(kind); setResult(undefined); setError(undefined); setStale(false); setCancelled(false)
    try {
      const response = await requestJson<ModelTestResult>('/api/models/configurations/test', {
        method: 'POST', body: { kind, configuration }, signal: controller.signal, suppressGlobalError: true,
      })
      if (!controller.signal.aborted && generation.current === sequence) setResult(response)
    } catch (reason) {
      if (!controller.signal.aborted && generation.current === sequence)
        setError(reason instanceof Error ? reason.message : '测试失败，请重试')
    } finally {
      if (active.current === controller) { active.current = null; setRunning(undefined) }
    }
  }
  return { running, result, error, stale, cancelled, run, invalidate, reset, cancel }
}
