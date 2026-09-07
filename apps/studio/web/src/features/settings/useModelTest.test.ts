import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { requestJson } from '../../api/shared/http'
import { newModel } from './useModelSettings'
import { useModelTest, type ModelTestConfiguration, type ModelTestResult } from './useModelTest'

vi.mock('../../api/shared/http', () => ({ requestJson: vi.fn() }))
const config: ModelTestConfiguration = {...newModel(), api_key: 'test-key'}
const result: ModelTestResult = {kind: 'text', outcome: 'success', elapsed_ms: 12, code: 'text_received', text: 'OK', image: null}

describe('草稿模型测试生命周期', () => {
  beforeEach(() => { vi.mocked(requestJson).mockReset() })
  it('仅调用测试接口，不保存草稿', async () => {
    vi.mocked(requestJson).mockResolvedValue(result)
    const hook = renderHook(() => useModelTest())
    await act(() => hook.result.current.run('text', config))
    expect(requestJson).toHaveBeenCalledTimes(1)
    expect(vi.mocked(requestJson).mock.calls[0]?.[0]).toBe('/api/models/configurations/test')
    expect(hook.result.current.result).toEqual(result)
    act(() => hook.result.current.invalidate())
    expect(hook.result.current.stale).toBe(true)
  })
  it('编辑后忽略不遵守取消信号的旧响应', async () => {
    let finish!: (value: ModelTestResult) => void
    vi.mocked(requestJson).mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const hook = renderHook(() => useModelTest())
    let pending!: Promise<void>
    act(() => { pending = hook.result.current.run('text', config) })
    act(() => hook.result.current.invalidate())
    await act(async () => { finish(result); await pending })
    expect(hook.result.current.result).toBeUndefined()
    expect(vi.mocked(requestJson).mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
  })
  it('重复点击只产生一次请求，取消后不继续发布结果', async () => {
    let finish!: (value: ModelTestResult) => void
    vi.mocked(requestJson).mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const hook = renderHook(() => useModelTest())
    let pending!: Promise<void>
    act(() => { pending = hook.result.current.run('text', config); void hook.result.current.run('text', config) })
    expect(requestJson).toHaveBeenCalledTimes(1)
    act(() => hook.result.current.cancel())
    await act(async () => { finish(result); await pending })
    expect(hook.result.current.cancelled).toBe(true)
    expect(hook.result.current.result).toBeUndefined()
  })
  it('卸载时取消请求', async () => {
    vi.mocked(requestJson).mockImplementation(() => new Promise(() => {}))
    const hook = renderHook(() => useModelTest())
    act(() => { void hook.result.current.run('text', config) })
    hook.unmount()
    expect(vi.mocked(requestJson).mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
  })
})
