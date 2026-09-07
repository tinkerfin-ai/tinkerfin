import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { attachmentBlob } from './client'
import { useAttachmentImage } from './useAttachmentImage'
import { useAttachmentDownload } from './useAttachmentDownload'
vi.mock('./client', () => ({ attachmentBlob: vi.fn() }))
const attachment = {
  id: 'a',
  name: '猫.png',
  mime_type: 'image/png',
  size_bytes: 3,
}
beforeEach(() => {
  vi.mocked(attachmentBlob).mockReset()
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:image'),
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  })
})
afterEach(() => vi.restoreAllMocks())
describe('图片资源所有权', () => {
  it('切换图片取消旧请求，延迟完成不能覆盖当前图片，卸载释放地址', async () => {
    let finish: (blob: Blob) => void = () => undefined
    vi.mocked(attachmentBlob)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve
          }),
      )
      .mockResolvedValueOnce(new Blob(['current']))
    const { result, rerender, unmount } = renderHook(
      ({ id }) => useAttachmentImage(id),
      { initialProps: { id: 'old' } },
    )
    const signal = vi.mocked(attachmentBlob).mock.calls[0][2]
    rerender({ id: 'current' })
    await waitFor(() => expect(result.current.url).toBe('blob:image'))
    expect(signal?.aborted).toBe(true)
    await act(async () => finish(new Blob(['stale'])))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:image')
  })
  it('读取及解码失败可重试，本地草稿不发网络请求', async () => {
    vi.mocked(attachmentBlob)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(new Blob(['image']))
    const { result, unmount } = renderHook(() => useAttachmentImage('a'))
    await waitFor(() => expect(result.current.failed).toBe(true))
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.url).toBeDefined())
    act(() => result.current.fail())
    expect(result.current.url).toBeUndefined()
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.url).toBeDefined())
    unmount()
    vi.mocked(attachmentBlob).mockClear()
    const file = new File(['image'], 'local.png')
    const local = renderHook(() =>
      useAttachmentImage(undefined, 'preview', file),
    )
    await waitFor(() => expect(local.result.current.url).toBeDefined())
    expect(attachmentBlob).not.toHaveBeenCalled()
    local.unmount()
  })
  it('下载失败可重试，保留文件名并阻止重复下载，切换附件取消请求', async () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.download).toBe('猫.png')
      })
    vi.mocked(attachmentBlob)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(new Blob(['image']))
    const { result, rerender, unmount } = renderHook(
      ({ item }) => useAttachmentDownload(item),
      { initialProps: { item: attachment } },
    )
    await act(async () => result.current.download())
    expect(result.current.failed).toBe(true)
    await act(async () => {
      void result.current.download()
      void result.current.download()
    })
    expect(click).toHaveBeenCalledTimes(1)
    expect(result.current.failed).toBe(false)
    vi.mocked(attachmentBlob).mockImplementationOnce(
      () => new Promise(() => undefined),
    )
    act(() => {
      void result.current.download()
    })
    const signal = vi.mocked(attachmentBlob).mock.calls.at(-1)?.[2]
    rerender({ item: { ...attachment, id: 'b' } })
    expect(signal?.aborted).toBe(true)
    expect(result.current.pending).toBe(false)
    unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:image')
  })
})
