import { useCallback, useRef, useState } from 'react'
import { useI18n } from '../../i18n'

const MAX_ATTACHMENT_COUNT = 5
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
const MAX_ATTACHMENT_TOTAL_BYTES = 25 * 1024 * 1024

const ATTACHMENT_KIND_BY_MIME = {
  'image/png': 'image',
  'image/jpeg': 'image',
  'image/webp': 'image',
  'image/gif': 'image',
  'application/pdf': 'pdf',
} as const

export type LocalAttachmentKind = 'image' | 'pdf'

export interface LocalAttachment {
  id: string
  file: File
  kind: LocalAttachmentKind
}

const attachmentKind = (file: File): LocalAttachmentKind | undefined => {
  const direct = ATTACHMENT_KIND_BY_MIME[file.type as keyof typeof ATTACHMENT_KIND_BY_MIME]
  if (direct) return direct
  if (file.type) return undefined
  const extension = file.name.toLowerCase().split('.').pop()
  if (extension === 'pdf') return 'pdf'
  if (['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(extension ?? '')) return 'image'
  return undefined
}

export function useLocalAttachments() {
  const { t } = useI18n()
  const [attachments, setAttachments] = useState<LocalAttachment[]>([])
  const [error, setError] = useState<string>()
  const latest = useRef(attachments)
  latest.current = attachments

  const addFiles = useCallback((files: readonly File[]) => {
    const accepted: LocalAttachment[] = []
    const rejected: string[] = []
    let nextCount = latest.current.length
    let nextBytes = latest.current.reduce((total, item) => total + item.file.size, 0)

    for (const file of files) {
      const kind = attachmentKind(file)
      if (!kind) {
        rejected.push(t('{name}：仅支持图片或 PDF', { name: file.name }))
        continue
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        rejected.push(t('{name}：单个文件不能超过 10MB', { name: file.name }))
        continue
      }
      if (nextCount >= MAX_ATTACHMENT_COUNT) {
        rejected.push(t('最多添加 5 个附件'))
        break
      }
      if (nextBytes + file.size > MAX_ATTACHMENT_TOTAL_BYTES) {
        rejected.push(t('附件总大小不能超过 25MB'))
        break
      }
      accepted.push({
        id: crypto.randomUUID(),
        file,
        kind,
      })
      nextCount += 1
      nextBytes += file.size
    }

    if (accepted.length) setAttachments((current) => [...current, ...accepted])
    setError(rejected[0])
  }, [t])

  const removeAttachment = useCallback((id: string) => {
    setAttachments((current) => current.filter((item) => item.id !== id))
    setError(undefined)
  }, [])

  const clearAttachments = useCallback(() => {
    latest.current = []
    setAttachments([])
    setError(undefined)
  }, [])

  return {
    attachments,
    error,
    addFiles,
    removeAttachment,
    clearAttachments,
  }
}
