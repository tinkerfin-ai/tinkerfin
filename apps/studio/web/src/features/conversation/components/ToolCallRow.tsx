import {
  Bot,
  ChevronDown,
  FilePenLine,
  FilePlus2,
  FileText,
  FolderSearch,
  FolderTree,
  Globe2,
  ListChecks,
  SquareTerminal,
  TextSearch,
  Trash2,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'

import type { Message } from '../../../types'
import { useI18n } from '../../../i18n'

type MessageStatus = NonNullable<Message['meta']>['status']

interface ToolPresentation {
  title: string
  icon: LucideIcon
  summaryKeys: readonly string[]
}

const TOOL_PRESENTATIONS: Record<string, ToolPresentation> = {
  ls: { title: 'List', icon: FolderTree, summaryKeys: ['path'] },
  read_file: { title: 'Read', icon: FileText, summaryKeys: ['file_path', 'path'] },
  write_file: { title: 'Write', icon: FilePlus2, summaryKeys: ['file_path', 'path'] },
  edit_file: { title: 'Edit', icon: FilePenLine, summaryKeys: ['file_path', 'path'] },
  delete: { title: 'Delete', icon: Trash2, summaryKeys: ['file_path', 'path'] },
  glob: { title: 'Glob', icon: FolderSearch, summaryKeys: ['pattern', 'path'] },
  grep: { title: 'Grep', icon: TextSearch, summaryKeys: ['pattern', 'query', 'path'] },
  execute: { title: 'Execute', icon: SquareTerminal, summaryKeys: ['description', 'command'] },
  web_search: { title: 'Search', icon: Globe2, summaryKeys: ['query'] },
  write_todos: { title: 'Todos', icon: ListChecks, summaryKeys: [] },
  task: { title: 'Task', icon: Bot, summaryKeys: ['description', 'subagent_type'] },
}

const firstLine = (value: string) => value.split(/\r?\n/, 1)[0]?.trim() ?? ''

const parseParams = (value: string): Record<string, unknown> | null => {
  try {
    const parsed: unknown = JSON.parse(value)
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

const firstStringValue = (params: Record<string, unknown>, keys: readonly string[]) => {
  for (const key of keys) {
    const value = params[key]
    if (typeof value === 'string' && value.trim()) return firstLine(value)
  }
  for (const value of Object.values(params)) {
    if (typeof value === 'string' && value.trim()) return firstLine(value)
  }
  return ''
}

const toolSummary = (
  message: Message,
  toolName: string,
  presentation: ToolPresentation | undefined,
) => {
  const params = message.meta?.params ?? ''
  const parsed = params ? parseParams(params) : null
  let summary = ''

  if (parsed && toolName === 'web_search' && Array.isArray(parsed.queries)) {
    summary = parsed.queries
      .filter((query): query is string => typeof query === 'string' && Boolean(query.trim()))
      .map(firstLine)
      .join(', ')
  }
  if (!summary && parsed) {
    summary = firstStringValue(parsed, presentation?.summaryKeys ?? [])
  }
  if (!summary && params) summary = firstLine(params)
  if (!summary) return presentation ? '' : toolName

  return presentation ? summary : `${toolName} · ${summary}`
}

const statusText = (status: MessageStatus | undefined, t: ReturnType<typeof useI18n>['t']) => {
  if (status === 'failed') return t('执行失败')
  if (status === 'cancelled') return t('已取消')
  if (status === 'paused') return t('等待审批')
  if (status === 'running') return t('正在运行')
  return t('已完成')
}

export function ToolCallRow({
  message,
  className,
  open,
  onOpenChange,
  children,
}: {
  message: Message
  className?: string
  open?: boolean
  onOpenChange?: (open: boolean) => void
  children: ReactNode
}) {
  const { t } = useI18n()
  const toolName = message.meta?.toolName?.trim() || 'tool'
  const presentation = TOOL_PRESENTATIONS[toolName]
  const ToolIcon = presentation?.icon ?? Wrench
  const status = message.meta?.status ?? 'completed'
  const failure = status === 'failed' && Boolean(message.meta?.result)
    ? firstLine(message.meta?.result ?? '')
    : ''
  const summary = failure || toolSummary(message, toolName, presentation)

  return (
    <details
      id={message.id}
      className={`tool-row ${status}${className ? ` ${className}` : ''}`}
      open={open}
      data-tool-name={toolName}
      onToggle={onOpenChange
        ? (event) => onOpenChange(event.currentTarget.open)
        : undefined}
    >
      <summary>
        <span className="tool-row-visually-hidden">{statusText(status, t)}</span>
        <span className="tool-row-leading" aria-hidden="true">
          <span className="tool-row-icon">
            {status === 'failed' || status === 'cancelled'
              ? <span className={`tool-row-state-dot is-${status}`} />
              : <ToolIcon size={14} strokeWidth={2} />}
          </span>
          <ChevronDown className="tool-row-chevron" size={14} strokeWidth={2} />
        </span>
        <span className="tool-row-title">{presentation?.title ?? 'Tool call'}</span>
        {summary
          ? <>
              <span className="tool-row-separator" aria-hidden="true" />
              <span className={`tool-row-summary${failure ? ' is-error' : ''}`}>{summary}</span>
            </>
          : null}
      </summary>
      {children}
    </details>
  )
}
