import { X } from 'lucide-react'
import { useId, useLayoutEffect, useRef } from 'react'
import type { KeyboardEvent, ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'

import { useI18n } from '../../i18n'
import { IconButton } from './IconButton'

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export interface DialogProps {
  open: boolean
  title: string
  description?: ReactNode
  children: ReactNode
  className?: string
  closeDisabled?: boolean
  restoreFocusTo?: HTMLElement | null
  initialFocusRef?: RefObject<HTMLElement | null>
  onClose: () => void
}

export function Dialog({
  open,
  title,
  description,
  children,
  className,
  closeDisabled = false,
  restoreFocusTo,
  initialFocusRef,
  onClose,
}: DialogProps) {
  const { t } = useI18n()
  const titleId = useId()
  const descriptionId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)

  useLayoutEffect(() => {
    if (!open) return
    restoreFocusRef.current = restoreFocusTo
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    ;(initialFocusRef?.current ?? closeRef.current ?? dialogRef.current)?.focus()
    return () => restoreFocusRef.current?.focus()
  }, [initialFocusRef, open, restoreFocusTo])

  if (!open) return null

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && !closeDisabled) {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
    )
    if (focusable.length === 0) {
      event.preventDefault()
      dialogRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && (
      document.activeElement === first
      || !dialogRef.current?.contains(document.activeElement)
    )) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return createPortal(
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !closeDisabled) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className={`modal-dialog${className ? ` ${className}` : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="modal-dialog-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && (
              <div id={descriptionId} className="modal-dialog-description">{description}</div>
            )}
          </div>
          <IconButton
            ref={closeRef}
            className="modal-dialog-close"
            label={t('关闭对话框')}
            icon={<X size={18} />}
            disabled={closeDisabled}
            onClick={onClose}
          />
        </header>
        {children}
      </div>
    </div>,
    document.body,
  )
}
