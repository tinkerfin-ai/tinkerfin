import type {
  TraceGraphNode,
  TraceGraphNodeKind,
} from '../../../api/conversation/traceGraph'
import type { useI18n } from '../../../i18n'

type Translate = ReturnType<typeof useI18n>['t']

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

export const traceNodeName = (node: TraceGraphNode, t: Translate) => (
  node.kind === 'agent' && node.name === 'Agent'
    ? t('主智能体')
    : node.kind === 'tool' ? 'ToolMessage' : node.name
)

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
