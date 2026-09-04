import type {
  TraceGraphNode,
  TraceGraphNodeKind,
} from '../../../api/conversation/traceGraph'
import type { useI18n } from '../../../i18n'
import type { JsonValue } from '../../../types'

type Translate = ReturnType<typeof useI18n>['t']
export type TracePublicCategory = 'user' | 'assistant' | 'tool' | 'context'
export type TraceVisualCategory = TracePublicCategory | 'technical'

export const TRACE_PUBLIC_CATEGORIES: readonly TracePublicCategory[] = [
  'user',
  'assistant',
  'tool',
  'context',
]

export const TRACE_PUBLIC_CATEGORY_KINDS: Readonly<Record<
  TracePublicCategory,
  readonly TraceGraphNodeKind[]
>> = {
  user: ['human_message', 'interaction'],
  assistant: ['assistant_message', 'subagent'],
  tool: ['tool'],
  context: ['skill', 'memory', 'guardrail', 'retrieval', 'custom', 'plan'],
}

export const TRACE_TECHNICAL_KINDS: readonly TraceGraphNodeKind[] = [
  'agent',
  'model',
  'system_message',
  'middleware',
  'run',
  'runtime_task',
]

export const traceKindLabel = (kind: TraceGraphNodeKind, t: Translate) => ({
  human_message: t('用户消息'),
  assistant_message: t('助手消息'),
  system_message: t('系统消息'),
  agent: t('智能体调用'),
  run: t('运行'),
  model: t('模型调用'),
  tool: t('工具'),
  subagent: t('子智能体'),
  skill: t('技能'),
  middleware: t('中间件'),
  memory: t('记忆'),
  guardrail: t('护栏'),
  retrieval: t('检索'),
  custom: t('上下文'),
  plan: t('计划'),
  interaction: t('人工交互'),
  runtime_task: t('运行时任务'),
}[kind])

export const traceVisualCategory = (kind: TraceGraphNodeKind): TraceVisualCategory => {
  switch (kind) {
    case 'human_message':
    case 'interaction':
      return 'user'
    case 'assistant_message':
    case 'subagent':
      return 'assistant'
    case 'tool':
      return 'tool'
    case 'skill':
    case 'memory':
    case 'guardrail':
    case 'retrieval':
    case 'custom':
    case 'plan':
      return 'context'
    default:
      return 'technical'
  }
}

export const traceVisualCategoryLabel = (category: TraceVisualCategory, t: Translate) => ({
  user: t('用户'),
  assistant: t('助手'),
  tool: t('工具'),
  context: t('上下文'),
  technical: t('技术'),
}[category])

export const traceKindCompactLabel = (kind: TraceGraphNodeKind, t: Translate) => (
  traceVisualCategoryLabel(traceVisualCategory(kind), t)
)

export const traceNodeName = (node: TraceGraphNode, t: Translate) => (
  node.kind === 'agent' && node.name === 'Agent'
    ? t('主智能体')
    : node.name
)

const compactJson = (value: JsonValue | null | undefined) => {
  if (value == null) return ''
  if (typeof value === 'string') return value.replaceAll(/\s+/g, ' ').trim()
  return JSON.stringify(value)
}

export const traceNodePreview = (node: TraceGraphNode) => {
  if (node.failure) return node.failure.message ?? node.failure.errorType
  if (node.kind.endsWith('_message')) return compactJson(node.content)
  if (node.kind === 'tool') return compactJson(node.request)
  return compactJson(node.result) || compactJson(node.request)
}

export const traceNodeAccessibleLabel = (node: TraceGraphNode, t: Translate) => {
  const category = traceVisualCategoryLabel(traceVisualCategory(node.kind), t)
  const value = node.kind.endsWith('_message')
    ? traceNodePreview(node)
    : node.kind === 'tool' ? node.name : traceNodeName(node, t)
  const summary = value.length > 96 ? `${value.slice(0, 95)}…` : value
  return `${category}，${summary || t('不可用')}`
}

export const traceStatusLabel = (status: TraceGraphNode['status'], t: Translate) => ({
  running: t('运行中'),
  waiting: t('等待中'),
  succeeded: t('已完成'),
  failed: t('失败'),
  cancelled: t('已取消'),
  abandoned: t('已放弃'),
  unknown: t('未知'),
}[status])

export const elapsedMilliseconds = (entry: TraceGraphNode) => {
  if (!entry.completedAt) return null
  return Math.max(0, Date.parse(entry.completedAt) - Date.parse(entry.startedAt))
}

export const durationLabel = (
  milliseconds: number | null,
  t: Translate,
) => {
  if (milliseconds == null) return t('进行中')
  if (milliseconds < 1000) {
    return t('{count} 毫秒', { count: Math.round(milliseconds) })
  }
  return t('{count} 秒', { count: (milliseconds / 1000).toFixed(2) })
}
