import { X } from 'lucide-react'
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import type { FormEvent, KeyboardEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { Button, IconButton, TextField } from '../../../components/ui'

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export interface ModalDialogProps {
  open: boolean
  title: string
  description?: ReactNode
  confirmLabel: string
  cancelLabel?: string
  tone?: 'default' | 'danger'
  inputLabel?: string
  inputPlaceholder?: string
  initialValue?: string
  isPending?: boolean
  error?: string
  restoreFocusTo?: HTMLElement | null
  onConfirm: (value?: string) => void | Promise<void>
  onCancel: () => void
}

export function ModalDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = '取消',
  tone = 'default',
  inputLabel,
  inputPlaceholder,
  initialValue = '',
  isPending = false,
  error,
  restoreFocusTo,
  onConfirm,
  onCancel,
}: ModalDialogProps) {
  const titleId = useId()
  const descriptionId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)
  const [value, setValue] = useState(initialValue)

  useEffect(() => {
    if (!open) return
    setValue(initialValue)
  }, [initialValue, open])

  useLayoutEffect(() => {
    if (!open) return
    restoreFocusRef.current = restoreFocusTo
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    ;(inputRef.current ?? cancelRef.current ?? dialogRef.current)?.focus()
    inputRef.current?.select()
    return () => restoreFocusRef.current?.focus()
  }, [open, restoreFocusTo])

  if (!open) return null

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && !isPending) {
      event.preventDefault()
      onCancel()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [])
    if (focusable.length === 0) {
      event.preventDefault()
      dialogRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (isPending || (inputLabel && !value.trim())) return
    void onConfirm(inputLabel ? value.trim() : undefined)
  }

  return createPortal(
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !isPending) onCancel()
      }}
    >
      <div
        ref={dialogRef}
        className={`modal-dialog is-${tone}`}
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
            {description && <div id={descriptionId} className="modal-dialog-description">{description}</div>}
          </div>
          <IconButton
            className="modal-dialog-close"
            label="关闭对话框"
            icon={<X size={17} />}
            disabled={isPending}
            onClick={onCancel}
          />
        </header>
        <form onSubmit={handleSubmit}>
          {inputLabel && (
            <TextField
              ref={inputRef}
              rootClassName="modal-dialog-field"
              label={inputLabel}
              value={value}
              placeholder={inputPlaceholder}
              disabled={isPending}
              loading={isPending}
              onChange={(event) => setValue(event.target.value)}
            />
          )}
          {error && <p className="modal-dialog-error" role="alert">{error}</p>}
          <footer className="modal-dialog-actions">
            <Button ref={cancelRef} variant="secondary" disabled={isPending} onClick={onCancel}>{cancelLabel}</Button>
            <Button
              type="submit"
              variant={tone === 'danger' ? 'danger' : 'primary'}
              disabled={isPending || Boolean(inputLabel && !value.trim())}
            >
              {isPending ? '处理中…' : confirmLabel}
            </Button>
          </footer>
        </form>
      </div>
    </div>,
    document.body,
  )
}
