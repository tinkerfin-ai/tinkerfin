import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useLocalAttachments } from './useLocalAttachments'

const file = (name: string, type: string, size = 128) => {
  const value = new File(['x'], name, { type })
  Object.defineProperty(value, 'size', { configurable: true, value: size })
  return value
}

describe('useLocalAttachments', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => `attachment-${Math.random()}`) })
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn((value: File) => `blob:${value.name}`),
      revokeObjectURL: vi.fn(),
    })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('accepts images and PDF while keeping previews local', () => {
    const { result } = renderHook(() => useLocalAttachments())

    act(() => result.current.addFiles([
      file('chart.png', 'image/png'),
      file('brief.pdf', 'application/pdf'),
    ]))

    expect(result.current.attachments).toHaveLength(2)
    expect(result.current.attachments[0]).toMatchObject({
      kind: 'image',
      previewUrl: 'blob:chart.png',
    })
    expect(result.current.attachments[1]).toMatchObject({ kind: 'pdf' })
    expect(result.current.error).toBeUndefined()
  })

  it('enforces type, item size, count and aggregate limits', () => {
    const { result } = renderHook(() => useLocalAttachments())

    act(() => result.current.addFiles([file('script.js', 'text/javascript')]))
    expect(result.current.error).toContain('仅支持图片或 PDF')

    act(() => result.current.addFiles([file('large.pdf', 'application/pdf', 10 * 1024 * 1024 + 1)]))
    expect(result.current.error).toContain('不能超过 10MB')

    act(() => result.current.addFiles(Array.from(
      { length: 6 },
      (_, index) => file(`${index}.pdf`, 'application/pdf'),
    )))
    expect(result.current.attachments).toHaveLength(5)
    expect(result.current.error).toBe('最多添加 5 个附件')
  })

  it('releases image URLs on removal, clear and unmount', () => {
    const { result, unmount } = renderHook(() => useLocalAttachments())

    act(() => result.current.addFiles([file('one.png', 'image/png')]))
    const first = result.current.attachments[0]
    if (!first) throw new Error('missing first attachment')
    act(() => result.current.removeAttachment(first.id))
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:one.png')

    act(() => result.current.addFiles([file('two.png', 'image/png')]))
    act(() => result.current.clearAttachments())
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:two.png')

    act(() => result.current.addFiles([file('three.png', 'image/png')]))
    unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:three.png')
  })
})
