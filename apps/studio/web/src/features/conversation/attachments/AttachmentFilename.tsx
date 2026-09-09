import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Button, MOTION_DURATION_MS } from '../../../components/ui'

/** 文件名被截断时显示完整名称；提示位于滚动区域外，支持悬浮、键盘和触控 */
export function AttachmentFilename({ name }: { name: string }) {
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const text = useRef<HTMLSpanElement>(null)
  const tooltip = useRef<HTMLSpanElement>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })

  const keepOpen = () => {
    clearTimeout(closeTimer.current)
    closeTimer.current = undefined
  }
  const reveal = () => {
    keepOpen()
    if (text.current && text.current.scrollWidth > text.current.clientWidth) setOpen(true)
  }
  const leave = () => {
    keepOpen()
    if (document.activeElement === trigger.current) return
    closeTimer.current = setTimeout(() => setOpen(false), MOTION_DURATION_MS.normal)
  }

  useEffect(() => () => clearTimeout(closeTimer.current), [])
  useLayoutEffect(() => {
    if (!open) return
    const button = trigger.current?.getBoundingClientRect()
    const tip = tooltip.current?.getBoundingClientRect()
    if (!button || !tip) return
    const tokens = getComputedStyle(document.documentElement)
    const inset = parseFloat(tokens.getPropertyValue('--space-3'))
    const gap = parseFloat(tokens.getPropertyValue('--space-1'))
    setPosition({
      left: Math.max(inset, Math.min(button.left, window.innerWidth - tip.width - inset)),
      top: button.top >= tip.height + gap + inset ? button.top - tip.height - gap : button.bottom + gap,
    })
  }, [open, name])
  useEffect(() => {
    if (!open) return
    const dismiss = () => setOpen(false)
    const pointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !trigger.current?.contains(event.target) && !tooltip.current?.contains(event.target)) dismiss()
    }
    const keyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        dismiss()
      }
    }
    window.addEventListener('resize', dismiss)
    window.addEventListener('scroll', dismiss, true)
    document.addEventListener('pointerdown', pointerDown)
    document.addEventListener('keydown', keyDown)
    return () => {
      window.removeEventListener('resize', dismiss)
      window.removeEventListener('scroll', dismiss, true)
      document.removeEventListener('pointerdown', pointerDown)
      document.removeEventListener('keydown', keyDown)
    }
  }, [open])

  return (
    <>
      <Button
        ref={trigger}
        type="button"
        variant="text"
        className="composer-attachment-name"
        aria-describedby={open ? id : undefined}
        onPointerEnter={reveal}
        onPointerLeave={leave}
        onFocus={reveal}
        onBlur={() => setOpen(false)}
        onClick={reveal}
      >
        <span ref={text} className="composer-attachment-name-text">{name}</span>
      </Button>
      {open && createPortal(
        <span
          ref={tooltip}
          id={id}
          role="tooltip"
          className="ui-tooltip composer-attachment-name-tooltip"
          style={position}
          onPointerEnter={keepOpen}
          onPointerLeave={leave}
        >
          {name}
        </span>,
        document.body,
      )}
    </>
  )
}
