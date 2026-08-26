import { ArrowUp, FileImage, FileText, Plus, Square, X } from 'lucide-react'
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

import { IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import {
  applyAtomicPlanDeletion,
  cancelComposerSuggestion,
  detectLeadingSlashToken,
  enabledSuggestionIds,
  filterComposerSuggestionGroups,
  isAllowedComposerDraft,
  isSubmittableComposerDraft,
  planClaimParts,
  replaceSlashTokenWithPlan,
} from '../composerSuggestions'
import type { LocalAttachment } from '../useLocalAttachments'
import { ComposerPlanChip } from './ComposerPlanChip'
import { ComposerSuggestionMenu } from './ComposerSuggestionMenu'

export function Composer({
  value,
  isRunning,
  canStop = true,
  stopPending = false,
  isHydrating = false,
  disabledReason,
  hero,
  takeover,
  modelControl,
  planActive,
  planLocked = false,
  attachments,
  attachmentError,
  onChange,
  onSend,
  onStop,
  onExitPlan,
  onAddAttachments,
  onRemoveAttachment,
  onScrollConversation,
}: {
  value: string
  isRunning: boolean
  canStop?: boolean
  stopPending?: boolean
  isHydrating?: boolean
  disabledReason?: string
  hero?: ReactNode
  takeover?: ReactNode
  modelControl: ReactNode
  planActive: boolean
  planLocked?: boolean
  attachments: readonly LocalAttachment[]
  attachmentError?: string
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  onExitPlan: () => void
  onAddAttachments: (files: readonly File[]) => void
  onRemoveAttachment: (id: string) => void
  onScrollConversation?: (deltaY: number) => void
}) {
  const { t } = useI18n()
  const input = useRef<HTMLTextAreaElement>(null)
  const inputScroll = useRef<HTMLDivElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const pendingCaret = useRef<number | null>(null)
  const menuId = `composer-suggestions-${useId()}`
  const [caret, setCaret] = useState(value.length)
  const [activeSuggestionId, setActiveSuggestionId] = useState<string>()
  const takeoverWasActive = useRef(Boolean(takeover))
  const isDisabled = isHydrating || Boolean(disabledReason)
  const slashHit = useMemo(
    () => isDisabled ? null : detectLeadingSlashToken(value, caret),
    [caret, isDisabled, value],
  )
  const suggestionGroups = useMemo(
    () => filterComposerSuggestionGroups(slashHit?.query ?? ''),
    [slashHit?.query],
  )
  const enabledIds = useMemo(
    () => enabledSuggestionIds(suggestionGroups),
    [suggestionGroups],
  )
  const menuOpen = Boolean(
    slashHit
    && suggestionGroups.some((group) => group.items.length > 0),
  )
  const resolvedActiveId = enabledIds.includes(activeSuggestionId ?? '')
    ? activeSuggestionId
    : enabledIds[0]
  const planClaim = planClaimParts(value)
  const canSubmitDraft = Boolean(value.trim()) && isSubmittableComposerDraft(value)
  const cancelSuggestionMenu = useCallback(() => {
    const cancellation = cancelComposerSuggestion(value)
    pendingCaret.current = cancellation.caret
    onChange(cancellation.value)
  }, [onChange, value])

  useLayoutEffect(() => {
    if (pendingCaret.current == null) return
    const nextCaret = pendingCaret.current
    pendingCaret.current = null
    input.current?.setSelectionRange(nextCaret, nextCaret)
    setCaret(nextCaret)
  }, [value])

  useEffect(() => {
    const wasActive = takeoverWasActive.current
    takeoverWasActive.current = Boolean(takeover)
    if (!wasActive || takeover || isDisabled) return
    const frame = window.requestAnimationFrame(() => input.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [isDisabled, takeover])

  const pickSuggestion = (id: string) => {
    if (id !== 'command-plan' || !slashHit) return
    const replacement = replaceSlashTokenWithPlan(value, slashHit)
    pendingCaret.current = replacement.caret
    onChange(replacement.value)
  }

  const moveSuggestion = (direction: 1 | -1) => {
    if (enabledIds.length === 0) return
    const currentIndex = Math.max(0, enabledIds.indexOf(resolvedActiveId ?? ''))
    setActiveSuggestionId(
      enabledIds[(currentIndex + direction + enabledIds.length) % enabledIds.length],
    )
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (menuOpen) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        moveSuggestion(event.key === 'ArrowDown' ? 1 : -1)
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        cancelSuggestionMenu()
        return
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && resolvedActiveId) {
        event.preventDefault()
        pickSuggestion(resolvedActiveId)
        return
      }
    }
    if (event.key === 'Backspace' || event.key === 'Delete') {
      const atomicEdit = applyAtomicPlanDeletion(
        value,
        event.currentTarget.selectionStart,
        event.currentTarget.selectionEnd,
        event.key === 'Backspace' ? 'backward' : 'forward',
      )
      if (atomicEdit) {
        event.preventDefault()
        pendingCaret.current = atomicEdit.caret
        onChange(atomicEdit.value)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
      event.preventDefault()
      if (canSubmitDraft && !isRunning && !isDisabled) onSend()
    }
  }

  return (
    <footer className={`composer-dock${hero ? ' is-hero' : ''}${takeover ? ' is-taken-over' : ''}`}>
      <div
        className={`composer-default${takeover ? ' is-taken-over' : ''}`}
        aria-hidden={Boolean(takeover) || undefined}
        inert={Boolean(takeover) || undefined}
      >
        {hero && <div className="composer-hero">{hero}</div>}
        <div className="composer" onPointerDown={(event) => {
        if (event.target instanceof Element && event.target.closest('button')) return
        input.current?.focus()
      }} onWheel={(event) => {
        const scroll = inputScroll.current
        if (scroll?.contains(event.target as Node) && scroll.scrollHeight > scroll.clientHeight) return
        if (!onScrollConversation) return
        event.preventDefault()
        onScrollConversation(event.deltaY)
      }}>
        {menuOpen && (
          <ComposerSuggestionMenu
            id={menuId}
            groups={suggestionGroups}
            activeId={resolvedActiveId}
            onPick={pickSuggestion}
            onDismiss={cancelSuggestionMenu}
          />
        )}
        {attachments.length > 0 && (
          <div className="composer-attachments" aria-label={t('本地附件')}>
            <span className="visually-hidden">{t('附件仅在本地预览，不会随消息发送')}</span>
            {attachments.map((attachment) => (
              <div
                key={attachment.id}
                className="composer-attachment"
                title={t('仅在本地预览，不会随消息发送')}
              >
                <span className="composer-attachment-icon" aria-hidden="true">
                  {attachment.kind === 'image' ? <FileImage size={16} /> : <FileText size={16} />}
                </span>
                <strong>{attachment.file.name}</strong>
                <button
                  type="button"
                  className="composer-attachment-remove"
                  aria-label={t('移除附件：{name}', { name: attachment.file.name })}
                  onClick={() => onRemoveAttachment(attachment.id)}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        )}
        {attachmentError && <p className="composer-attachment-error" role="status" aria-live="polite">{attachmentError}</p>}
        <div ref={inputScroll} className="composer-input-scroll">
          <div className="composer-input-grow">
            <div className={`composer-input-backdrop${isDisabled ? ' is-disabled' : ''}`} aria-hidden="true">
              {planClaim ? (
                <>
                  {planClaim.leading}
                  <mark>{planClaim.token}</mark>
                  {planClaim.content || <span>{t('描述你的任务以生成计划')}</span>}
                </>
              ) : value}
            </div>
            <textarea
              ref={input}
              className="composer-input"
              aria-label={t('消息输入')}
              aria-busy={isHydrating}
              aria-controls={menuOpen ? menuId : undefined}
              aria-activedescendant={menuOpen && resolvedActiveId ? `${menuId}-${resolvedActiveId}` : undefined}
              aria-autocomplete="list"
              disabled={isDisabled}
              value={value}
              onChange={(event) => {
                if (!isAllowedComposerDraft(event.target.value)) return
                setCaret(event.target.selectionStart)
                onChange(event.target.value)
              }}
              onSelect={(event) => setCaret(event.currentTarget.selectionStart)}
              onKeyDown={handleKeyDown}
              rows={1}
              placeholder={isHydrating ? t('正在加载会话…') : disabledReason ?? t('给 TinkerFin 发消息')}
            />
            <div className="composer-input-mirror" aria-hidden="true">{`${value}\n`}</div>
          </div>
        </div>
        <div className="composer-toolbar">
          <div className="composer-toolbar-leading">
            <input
              ref={fileInput}
              type="file"
              hidden
              multiple
              accept="image/png,image/jpeg,image/webp,image/gif,application/pdf"
              onChange={(event) => {
                onAddAttachments(Array.from(event.target.files ?? []))
                event.target.value = ''
              }}
            />
            <IconButton
              size="sm"
              className="composer-add-button"
              label={t('添加本地附件')}
              tooltip={t('添加本地附件')}
              icon={<Plus size={18} />}
              onClick={() => fileInput.current?.click()}
            />
            {planActive && (
              <ComposerPlanChip locked={planLocked} onExitPlan={onExitPlan} />
            )}
          </div>
          <div className="composer-toolbar-trailing">
            {modelControl}
            {isRunning ? (
              <IconButton
                className="send-button stop"
                label={stopPending ? t('正在停止任务') : canStop ? t('停止任务') : t('正在创建会话')}
                icon={<Square size={13} fill="currentColor" />}
                loading={stopPending}
                disabled={!canStop || stopPending}
                onClick={onStop}
              />
            ) : (
              <IconButton className="send-button" label={t('发送消息')} icon={<ArrowUp size={18} />} disabled={isDisabled || !canSubmitDraft} onClick={onSend} />
            )}
          </div>
        </div>
        </div>
      </div>
      {takeover && <div className="composer-takeover">{takeover}</div>}
      <p className="composer-note">{t('TinkerFin 可能会犯错，请核对重要信息')}</p>
    </footer>
  )
}
