import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useAttachments } from './useAttachments'
import { removeDraftAttachment, uploadAttachment } from './attachments/client'

vi.mock('./attachments/client', () => ({
  uploadAttachment: vi.fn(),
  removeDraftAttachment: vi.fn().mockResolvedValue(undefined),
}))

describe('attachment uploads', () => {
  it('retains a failed file and retries it into a ready attachment', async () => {
    vi.mocked(uploadAttachment)
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({
        id: 'stored',
        name: 'chart.png',
        mime_type: 'image/png',
        size_bytes: 3,
      })
    const onError = vi.fn()
    const { result } = renderHook(() => useAttachments(onError))
    act(() =>
      result.current.addFiles([
        new File(['png'], 'chart.png', { type: 'image/png' }),
      ]),
    )
    await waitFor(() =>
      expect(result.current.attachments[0]?.state).toBe('error'),
    )
    expect(onError).toHaveBeenCalledExactlyOnceWith('network')
    act(() => result.current.retryAttachment(result.current.attachments[0].id))
    await waitFor(() =>
      expect(result.current.attachments[0]?.state).toBe('ready'),
    )
    expect(result.current.attachments[0].attachment?.id).toBe('stored')
  })
  it('rejects unsupported files and oversized images without uploading', () => {
    vi.mocked(uploadAttachment).mockClear()
    const { result } = renderHook(() => useAttachments())
    act(() =>
      result.current.addFiles([
        new File(['x'], 'macro.exe'),
        new File([new Uint8Array(10 * 1024 ** 2 + 1)], 'big.png'),
      ]),
    )
    expect(result.current.attachments).toEqual([])
    expect(result.current.error).toBeTruthy()
    expect(uploadAttachment).not.toHaveBeenCalled()
  })
  it('does not put an old deletion error into a new conversation draft', async () => {
    let rejectDelete: (error: Error) => void = () => undefined
    vi.mocked(uploadAttachment).mockResolvedValueOnce({id: 'stored', name: 'a.png', mime_type: 'image/png', size_bytes: 3})
    vi.mocked(removeDraftAttachment).mockImplementationOnce(() => new Promise((_, reject) => { rejectDelete = reject }))
    const onError = vi.fn()
    const { result } = renderHook(() => useAttachments(onError))
    act(() => result.current.addFiles([new File(['png'], 'a.png')]))
    await waitFor(() => expect(result.current.attachments[0]?.state).toBe('ready'))
    act(() => result.current.removeAttachment(result.current.attachments[0].id))
    act(() => result.current.clearAttachments())
    await act(async () => rejectDelete(new Error('old deletion failed')))
    expect(result.current.error).toBeUndefined()
    expect(onError).not.toHaveBeenCalled()
  })

})
