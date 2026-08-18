import { ArrowUp, Square } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'

const MIN_INPUT_HEIGHT = 24
const MAX_INPUT_HEIGHT = 144

export function Composer({
  value,
  isRunning,
  canStop = true,
  isHydrating = false,
  onChange,
  onSend,
  onStop,
}: {
  value: string
  isRunning: boolean
  canStop?: boolean
  isHydrating?: boolean
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
}) {
  const input = useRef<HTMLTextAreaElement>(null)

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
          disabled={isHydrating}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
              event.preventDefault()
              if (value.trim() && !isRunning && !isHydrating) onSend()
            }
          }}
          rows={1}
          placeholder={isHydrating ? '正在加载会话…' : '给 TinkerFin 发消息'}
        />
        {isRunning ? (
          <button
            className="send-button stop"
            aria-label={canStop ? '停止任务' : '正在创建会话'}
            disabled={!canStop}
            onClick={onStop}
          ><Square size={13} fill="currentColor" /></button>
        ) : (
          <button className="send-button" aria-label="发送消息" disabled={isHydrating || !value.trim()} onClick={onSend}><ArrowUp size={19} strokeWidth={2.4} /></button>
        )}
      </div>
      <p className="composer-note">TinkerFin 可能会犯错，请核对重要信息。</p>
    </div>
  )
}
