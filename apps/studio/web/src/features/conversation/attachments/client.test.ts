import { describe, expect, it, vi } from 'vitest'
import { apiClient } from '../../../api/shared/http'
import { attachmentBlob } from './client'
vi.mock('../../../api/shared/http', () => ({ apiClient: { request: vi.fn() } }))
describe('附件读取等待边界', () => {
  it.each([
    ['preview', 30_000],
    ['original', 60_000],
  ] as const)('%s 设置有限等待并保留取消信号', async (variant, timeout) => {
    const blob = new Blob(['image'])
    vi.mocked(apiClient.request).mockResolvedValueOnce({ data: blob })
    const controller = new AbortController()
    expect(await attachmentBlob('a/b', variant, controller.signal)).toBe(blob)
    expect(apiClient.request).toHaveBeenLastCalledWith(
      expect.objectContaining({
        url: '/api/attachments/a%2Fb/content',
        params: { variant },
        timeout,
        signal: controller.signal,
        responseType: 'blob',
        suppressGlobalError: true,
      }),
    )
  })
})
