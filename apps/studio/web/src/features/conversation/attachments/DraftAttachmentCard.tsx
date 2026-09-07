import { FileImage, FileText, RotateCcw, X } from 'lucide-react'
import { IconButton } from '../../../components/ui'
import { isTranslationKey, useI18n } from '../../../i18n'
import type { DraftAttachment } from '../useAttachments'
import { useAttachmentImage } from './useAttachmentImage'
import './attachments.css'

export function DraftAttachmentCard({
  attachment,
  onRemove,
  onRetry,
}: {
  attachment: DraftAttachment
  onRemove: (id: string) => void
  onRetry?: (id: string) => void
}) {
  const { t } = useI18n()
  const image = useAttachmentImage(
    attachment.kind === 'image' ? attachment.attachment?.id : undefined,
    'preview',
    attachment.kind === 'image' ? attachment.file : undefined,
  )
  const error = attachment.error ?? '上传失败，请重试'
  return (
    <div
      className={`composer-attachment${attachment.kind === 'image' ? ' composer-attachment--image' : ''}`}
    >
      {image.url ? (
        <img
          className="composer-attachment-thumbnail"
          src={image.url}
          alt={attachment.name}
          onError={image.fail}
        />
      ) : (
        <span className="composer-attachment-icon" aria-hidden="true">
          {attachment.kind === 'image' ? (
            <FileImage size={16} />
          ) : (
            <FileText size={16} />
          )}
        </span>
      )}
      <div className="composer-attachment-copy">
        <strong>{attachment.name}</strong>
        {attachment.state !== 'ready' && (
          <span
            className="composer-attachment-state"
            data-error={attachment.state === 'error'}
            role="status"
          >
            {attachment.state === 'error'
              ? isTranslationKey(error)
                ? t(error)
                : error
              : attachment.state === 'queued'
                ? t('等待上传')
                : `${attachment.progress}%`}
          </span>
        )}
      </div>
      {image.failed && (
        <IconButton
          type="button"
          variant="ghost"
          label={t('重新加载缩略图')}
          icon={<RotateCcw size={16} />}
          onClick={image.retry}
        />
      )}
      {attachment.state === 'error' && onRetry && (
        <IconButton
          type="button"
          variant="ghost"
          label={t('重试附件：{name}', { name: attachment.name })}
          icon={<RotateCcw size={16} />}
          onClick={() => onRetry(attachment.id)}
        />
      )}
      <IconButton
        type="button"
        variant="ghost"
        className="composer-attachment-remove-control"
        label={t('移除附件：{name}', { name: attachment.name })}
        icon={<X size={14} />}
        onClick={() => onRemove(attachment.id)}
      />
    </div>
  )
}
