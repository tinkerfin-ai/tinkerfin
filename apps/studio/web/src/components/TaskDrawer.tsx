import {
  ArrowRight,
  Check,
  Circle,
  ClipboardList,
  GripHorizontal,
  ListChecks,
  LoaderCircle,
  X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent, PointerEvent } from 'react'

import type { Conversation, TodoStatus } from '../types'
import { MarkdownContent } from './MarkdownContent'

const statusLabel: Record<TodoStatus, string> = {
  pending: '待执行',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
}

const SPLITTER_SIZE = 10
const DRAWER_PADDING = 12
const MIN_PANEL_HEIGHT = 180
const KEYBOARD_STEP = 24
const MIN_SPLIT_RATIO = 20
const MAX_SPLIT_RATIO = 80
const TASK_DRAWER_SPLIT_KEY_PREFIX = 'tinkerfin:task-drawer-split:'

const splitStorageKey = (threadId: string) => `${TASK_DRAWER_SPLIT_KEY_PREFIX}${threadId}`

const readSplitRatio = (threadId: string) => {
  if (!threadId) return 50
  try {
    const value = Number(window.sessionStorage.getItem(splitStorageKey(threadId)))
    return Number.isFinite(value) && value >= MIN_SPLIT_RATIO && value <= MAX_SPLIT_RATIO
      ? value
      : 50
  } catch {
    return 50
  }
}

const writeSplitRatio = (threadId: string, ratio: number) => {
  if (!threadId) return
  try {
    window.sessionStorage.setItem(splitStorageKey(threadId), String(ratio))
  } catch {
    // 浏览器禁用会话存储时仅保留当前挂载周期内的比例
  }
}

export function TaskDrawer({
  conversation,
}: {
  conversation: Conversation
}) {
  const drawerRef = useRef<HTMLElement>(null)
  const resizeMeasurement = useRef<{ top: number; contentHeight: number } | null>(null)
  const pendingPointerY = useRef<number | null>(null)
  const resizeFrame = useRef<number | null>(null)
  const resizingRef = useRef(false)
  const [splitRatio, setSplitRatio] = useState(() => readSplitRatio(conversation.threadId))
  const [isResizing, setResizing] = useState(false)

  const contentHeightFromMeasurement = (measured: number) => (
    Math.max((measured || 800) - DRAWER_PADDING, MIN_PANEL_HEIGHT * 2 + SPLITTER_SIZE)
  )

  const getContentHeight = () => {
    const measured = drawerRef.current?.getBoundingClientRect().height ?? 0
    return contentHeightFromMeasurement(measured)
  }

  useEffect(() => {
    setSplitRatio(readSplitRatio(conversation.threadId))
  }, [conversation.threadId])

  useEffect(() => {
    writeSplitRatio(conversation.threadId, splitRatio)
  }, [conversation.threadId, splitRatio])

  useEffect(() => () => {
    resizingRef.current = false
    if (resizeFrame.current != null) window.cancelAnimationFrame(resizeFrame.current)
    resizeFrame.current = null
  }, [])

  const setPlanHeight = (nextHeight: number, contentHeight = getContentHeight()) => {
    const availableHeight = contentHeight - SPLITTER_SIZE
    const height = Math.min(Math.max(nextHeight, MIN_PANEL_HEIGHT), availableHeight - MIN_PANEL_HEIGHT)
    const ratio = Math.min(
      MAX_SPLIT_RATIO,
      Math.max(MIN_SPLIT_RATIO, (height / contentHeight) * 100),
    )
    setSplitRatio(ratio)
  }

  const resizeFromPointer = (event: PointerEvent<HTMLDivElement>) => {
    if (!resizingRef.current || !resizeMeasurement.current) return
    pendingPointerY.current = event.clientY
    if (resizeFrame.current != null) return
    resizeFrame.current = window.requestAnimationFrame(() => {
      resizeFrame.current = null
      const clientY = pendingPointerY.current
      const measurement = resizeMeasurement.current
      pendingPointerY.current = null
      if (clientY == null || !measurement) return
      setPlanHeight(
        clientY - measurement.top - DRAWER_PADDING / 2,
        measurement.contentHeight,
      )
    })
  }

  const resizeFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const contentHeight = getContentHeight()
    const currentHeight = (splitRatio / 100) * contentHeight
    if (event.key === 'ArrowUp') setPlanHeight(currentHeight - KEYBOARD_STEP)
    else if (event.key === 'ArrowDown') setPlanHeight(currentHeight + KEYBOARD_STEP)
    else if (event.key === 'Home') setPlanHeight(MIN_PANEL_HEIGHT)
    else if (event.key === 'End') setPlanHeight(contentHeight - SPLITTER_SIZE - MIN_PANEL_HEIGHT)
    else return
    event.preventDefault()
  }

  const goToMessage = (messageId?: string) => {
    if (!messageId) return
    document.getElementById(messageId)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  const completedCount = conversation.todos.filter((todo) => todo.status === 'completed').length
  const runningCount = conversation.todos.filter((todo) => todo.status === 'running').length
  const progress = conversation.todos.length
    ? ((completedCount + runningCount * .5) / conversation.todos.length) * 100
    : 0
  const hasPlan = Boolean(conversation.plan)

  return (
    <aside
      ref={drawerRef}
      className={`task-drawer ${isResizing ? 'is-resizing' : ''} ${hasPlan ? '' : 'is-todo-only'}`}
      aria-label="任务抽屉"
      style={{ '--split-ratio': splitRatio } as CSSProperties}
    >
      {hasPlan && (
        <section className="drawer-panel plan-panel" aria-label="计划">
          <header className="panel-head">
            <div className="panel-title"><ListChecks size={20} /><h2>计划</h2></div>
          </header>
          <div className="panel-scroll">
            <div className="stacked-plan">
              <p className="plan-summary">{conversation.plan?.goal}</p>
              <ol>
                {conversation.plan?.steps.map((step, index) => (
                  <li key={step.title} className="plan-step">
                    <div className="plan-step-copy"><span><small>#{index + 1}</small><strong>{step.title}</strong></span><p>{step.detail}</p></div>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </section>
      )}

      {hasPlan && (
        <div
          className="drawer-splitter"
          role="separator"
          aria-label="调整计划和待办区域高度"
          aria-orientation="horizontal"
          aria-valuemin={20}
          aria-valuemax={80}
          aria-valuenow={Math.round(splitRatio)}
          aria-valuetext={`计划区域占比 ${Math.round(splitRatio)}%`}
          tabIndex={0}
          onKeyDown={resizeFromKeyboard}
          onPointerDown={(event) => {
            event.preventDefault()
            event.currentTarget.setPointerCapture?.(event.pointerId)
            const bounds = drawerRef.current?.getBoundingClientRect()
            if (bounds) {
              resizeMeasurement.current = {
                top: bounds.top,
                contentHeight: contentHeightFromMeasurement(bounds.height),
              }
            }
            resizingRef.current = true
            setResizing(true)
          }}
          onPointerMove={resizeFromPointer}
          onPointerUp={(event) => {
            event.currentTarget.releasePointerCapture?.(event.pointerId)
            resizingRef.current = false
            setResizing(false)
          }}
          onPointerCancel={() => {
            resizingRef.current = false
            setResizing(false)
          }}
        >
          <GripHorizontal size={18} aria-hidden="true" />
        </div>
      )}

      <section className="drawer-panel todo-panel" aria-label="待办清单">
        <header className="panel-head">
          <div className="panel-title"><ClipboardList size={20} /><h2>待办清单</h2></div>
        </header>
        <p className="panel-description">Agent 执行任务的实时进度。</p>
        <div className="panel-scroll todo-panel-scroll">
          {conversation.todos.length ? (
            <div className="todo-list">
              <div className="progress-card">
                <div><span>任务进度</span><strong>{completedCount}/{conversation.todos.length}{runningCount > 0 ? ' · 执行中' : ''}</strong></div>
                <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
              </div>
              {conversation.todos.map((todo, index) => (
                <button key={todo.id} className={`todo-item is-${todo.status}`} onClick={() => goToMessage(todo.targetMessageId)}>
                  <span className="todo-state">{todo.status === 'completed' ? <Check size={14} /> : todo.status === 'running' ? <LoaderCircle className="spin" size={14} /> : todo.status === 'failed' ? <X size={14} /> : <Circle size={13} />}</span>
                  <span className="todo-copy"><small>步骤 {index + 1} · {statusLabel[todo.status]}</small><strong>{todo.content}</strong>{todo.result && <div className="todo-result"><MarkdownContent content={todo.result} /></div>}</span>
                  {todo.targetMessageId && <ArrowRight size={15} className="todo-arrow" />}
                </button>
              ))}
            </div>
          ) : (
            <div className="panel-empty"><ClipboardList size={20} /><h3>暂无待办</h3><p>Agent 生成的执行任务会显示在这里。</p></div>
          )}
        </div>
      </section>
    </aside>
  )
}
