import {
  BookOpen,
  Bot,
  Cpu,
  Database,
  GitFork,
  Link2,
  MessageCircle,
  MessageCircleMore,
  Puzzle,
  Search,
  ShieldCheck,
  SquareCode,
  WandSparkles,
  Waypoints,
} from 'lucide-react'

import type {
  TraceGraphNode,
  TraceGraphNodeKind,
} from '../../../api/conversation/traceGraph'
import { useI18n } from '../../../i18n'
import {
  durationLabel,
  elapsedMilliseconds,
  traceKindCompactLabel,
  traceNodePreview,
  traceNodeName,
  traceVisualCategory,
} from './tracePresentation'

const nodeIcon = (kind: TraceGraphNodeKind) => {
  switch (kind) {
    case 'human_message': return <MessageCircleMore size={14} aria-hidden="true" />
    case 'assistant_message': return <MessageCircle size={14} aria-hidden="true" />
    case 'system_message': return <SquareCode size={14} aria-hidden="true" />
    case 'agent': return <WandSparkles size={14} aria-hidden="true" />
    case 'model': return <Cpu size={14} aria-hidden="true" />
    case 'tool': return <Link2 size={14} aria-hidden="true" />
    case 'subagent': return <GitFork size={14} aria-hidden="true" />
    case 'skill': return <BookOpen size={14} aria-hidden="true" />
    case 'memory': return <Database size={14} aria-hidden="true" />
    case 'retrieval': return <Search size={14} aria-hidden="true" />
    case 'guardrail': return <ShieldCheck size={14} aria-hidden="true" />
    case 'middleware': return <Link2 size={14} aria-hidden="true" />
    case 'run': return <Bot size={14} aria-hidden="true" />
    case 'runtime_task': return <Waypoints size={14} aria-hidden="true" />
    default: return <Puzzle size={14} aria-hidden="true" />
  }
}

export function TraceNodeIcon({ kind }: { kind: TraceGraphNodeKind }) {
  const category = traceVisualCategory(kind)
  return (
    <span className={`chain-trace-entry-icon is-${kind} is-category-${category}`}>
      {nodeIcon(kind)}
    </span>
  )
}

export function TraceNodeCopy({
  node,
  showKind = true,
  showPreview = true,
}: {
  node: TraceGraphNode
  showKind?: boolean
  showPreview?: boolean
}) {
  const { t } = useI18n()
  const preview = traceNodePreview(node)
  const title = node.kind.endsWith('_message')
    ? ''
    : node.kind === 'tool' ? node.name : traceNodeName(node, t)
  return (
    <span className="chain-trace-node-copy">
      {(title || showKind) && (
        <span className="chain-trace-node-title">
          {title && <strong>{title}</strong>}
          {showKind && <small>{traceKindCompactLabel(node.kind, t)}</small>}
        </span>
      )}
      {showPreview && preview && (
        <span className={[
          'chain-trace-node-content',
          node.failure ? 'is-error' : '',
          !title ? 'is-primary' : '',
        ].filter(Boolean).join(' ')}>
          {preview}
        </span>
      )}
    </span>
  )
}

export function TraceNodeType({ node }: { node: TraceGraphNode }) {
  const { t } = useI18n()
  const category = traceVisualCategory(node.kind)
  return (
    <span className={`chain-trace-type-pill is-category-${category}`}>
      {traceKindCompactLabel(node.kind, t)}
    </span>
  )
}

export function TraceNodeMeta({ node }: { node: TraceGraphNode }) {
  const { t } = useI18n()
  return (
    <span className="chain-trace-node-meta">
      {node.failure && <span className="chain-trace-error-badge">{t('错误')}</span>}
      {!node.failure && node.status === 'waiting' && (
        <span className="chain-trace-waiting-badge">{t('等待中')}</span>
      )}
      {!node.failure && node.status === 'running' && (
        <span className="chain-trace-running-badge">{t('运行中')}</span>
      )}
      <span className="chain-trace-duration">
        {durationLabel(elapsedMilliseconds(node), t)}
      </span>
    </span>
  )
}
