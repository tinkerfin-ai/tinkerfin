import { Check, ChevronDown } from 'lucide-react'

import { Button, ListboxPicker } from '../../../components/ui'
import { OverflowMarquee } from './OverflowMarquee'
import { useI18n } from '../../../i18n'

function ModelOption({ label, selected }: { label: string; selected: boolean }) {
  return (
    <>
      <OverflowMarquee className="model-option-label">{label}</OverflowMarquee>
      <span className="model-option-check" aria-hidden="true">{selected && <Check size={14} />}</span>
    </>
  )
}

export function ComposerModelPicker({
  model,
  modelIds,
  defaultModelId,
  modelDisplayName,
  status,
  open,
  onOpenChange,
  onSelectModel,
  onRetry,
}: {
  model: string
  modelIds: string[]
  defaultModelId: string
  modelDisplayName: (modelId: string) => string
  status: 'loading' | 'ready' | 'error'
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelectModel: (model: string) => void
  onRetry: () => void
}) {
  const { t } = useI18n()
  if (status === 'error') {
    return (
      <div className="composer-model-error" role="alert">
        <span>{t('模型加载失败')}</span>
        <Button size="sm" variant="text" onClick={onRetry}>{t('重试')}</Button>
      </div>
    )
  }

  const selectedModel = modelIds.includes(model) ? model : defaultModelId
  return (
    <ListboxPicker
      value={selectedModel}
      options={modelIds}
      open={open}
      onOpenChange={onOpenChange}
      onChange={onSelectModel}
      disabled={status !== 'ready' || modelIds.length === 0}
      triggerLabel={t('选择模型')}
      listboxLabel={t('模型选项')}
      rootClassName="composer-model-picker"
      triggerClassName="composer-model-select"
      listboxClassName="composer-model-options"
      optionClassName="overflow-marquee-trigger"
      renderTrigger={(selected) => (
        <>
          <span>{modelDisplayName(selected) || t('加载模型…')}</span>
          <ChevronDown size={14} />
        </>
      )}
      renderOption={(option, selected) => (
        <ModelOption label={modelDisplayName(option)} selected={selected} />
      )}
    />
  )
}
