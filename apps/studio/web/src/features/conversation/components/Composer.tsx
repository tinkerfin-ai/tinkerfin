import { ArrowUp, Square } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'

import { IconButton } from '../../../components/ui'

const MIN_INPUT_HEIGHT = 24
const MAX_INPUT_HEIGHT = 144

export function Composer({
  value,
  isRunning,
  canStop = true,
  isHydrating = false,
  disabledReason,
  onChange,
  onSend,
  onStop,
}: {
  value: string
  isRunning: boolean
  canStop?: boolean
  isHydrating?: boolean
  disabledReason?: string
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
}) {
  const input = useRef<HTMLTextAreaElement>(null)
  const isDisabled = isHydrating || Boolean(disabledReason)

  useLayoutEffect(() => {
    const textarea = input.current
    if (!textarea) return

    textarea.style.height = `${MIN_INPUT_HEIGHT}px`
    const nextHeight = Math.min(MAX_INPUT_HEIGHT, Math.max(MIN_INPUT_HEIGHT, textarea.scrollHeight))
    textarea.style.height = `${nextHeight}px`
    textarea.style.overflowY = textarea.scrollHeight > MAX_INPUT_HEIGHT ? 'auto' : 'hidden'
  }, [value])

  return (
    <div className="composer-wrap" onPointerDown={(event) => {
      if (event.target instanceof Element && event.target.closest('button')) return
      input.current?.focus()
    }}>
      <div className="composer">
        <textarea
          ref={input}
          aria-label="消息输入"
          aria-busy={isHydrating}
          disabled={isDisabled}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
              event.preventDefault()
              if (value.trim() && !isRunning && !isDisabled) onSend()
            }
          }}
          rows={1}
          placeholder={isHydrating ? '正在加载会话…' : disabledReason ?? '给 TinkerFin 发消息'}
        />
        {isRunning ? (
          <IconButton
            className="send-button stop"
            label={canStop ? '停止任务' : '正在创建会话'}
            icon={<Square size={13} fill="currentColor" />}
            disabled={!canStop}
            onClick={onStop}
          />
        ) : (
          <IconButton className="send-button" label="发送消息" icon={<ArrowUp size={18} />} disabled={isDisabled || !value.trim()} onClick={onSend} />
        )}
      </div>
      <p className="composer-note">TinkerFin 可能会犯错，请核对重要信息。</p>
    </div>
  )
}
