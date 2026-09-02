import type { TraceEntry, TraceEntryKind } from '../../../api/conversation/traceEntries'
import type { useI18n } from '../../../i18n'

type Translate = ReturnType<typeof useI18n>['t']

export const traceKindLabel = (kind: TraceEntryKind, t: Translate) => ({
  agent: t('智能体调用'),
  run: t('运行'),
  model: t('模型'),
  provider: t('模型请求'),
  tools: t('工具阶段'),
  tool_proposal: t('工具提议'),
  tool: t('工具执行'),
  subagent: t('子智能体'),
  skill: t('技能'),
  middleware: t('中间件'),
  memory: t('记忆'),
  guardrail: t('护栏'),
  retrieval: t('检索'),
  custom: t('上下文'),
  task: t('运行时任务'),
}[kind])

export const traceStatusLabel = (status: TraceEntry['status'], t: Translate) => ({
  configured: t('仅配置'),
  running: t('运行中'),
  waiting: t('等待中'),
  succeeded: t('已完成'),
  failed: t('失败'),
  cancelled: t('已取消'),
  abandoned: t('已放弃'),
  unknown: t('未知'),
}[status])

export const elapsedMilliseconds = (entry: TraceEntry) => {
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
