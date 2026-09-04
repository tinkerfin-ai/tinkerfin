import { ChevronDown } from 'lucide-react'
import { useRef, useState } from 'react'

import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { OverlayScrollbar } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { TraceTurnRows } from './traceLayout'
import {
  durationLabel,
  traceNodeAccessibleLabel,
} from './tracePresentation'
import {
  TraceNodeCopy,
  TraceNodeMeta,
  TraceNodeType,
} from './TraceNodeVisual'

export function TraceLedger({
  groups,
  now,
  selectedId,
  backgroundInert,
  onSelect,
}: {
  groups: TraceTurnRows[]
  now: number
  selectedId?: string
  backgroundInert: boolean
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [collapsedTurnIds, setCollapsedTurnIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )

  const toggleTurn = (turnId: string) => setCollapsedTurnIds((current) => {
    const next = new Set(current)
    if (next.has(turnId)) next.delete(turnId)
    else next.add(turnId)
    return next
  })

  return (
    <div className="chain-trace-ledger-host">
      <div
        ref={scrollRef}
        className="chain-trace-ledger"
        aria-label={t('链路节点')}
        aria-hidden={backgroundInert || undefined}
        inert={backgroundInert || undefined}
        tabIndex={-1}
      >
        <div className="chain-trace-ledger-header" aria-hidden="true">
          <span>{t('节点')}</span>
          <span>{t('类型')}</span>
          <span>{t('节点与内容预览')}</span>
          <span>{t('耗时')}</span>
        </div>
        {groups.map(({ turn, nodes }) => {
          const collapsed = collapsedTurnIds.has(turn.id)
          const contentId = `trace-ledger-${turn.id}`
          const startedAt = Date.parse(turn.startedAt)
          const completedAt = nodes.reduce((latest, node) => Math.max(
            latest,
            node.completedAt ? Date.parse(node.completedAt) : now,
          ), startedAt)
          return (
            <section key={turn.id} className="chain-trace-ledger-turn" aria-label={t('第 {count} 轮', { count: turn.ordinal })}>
              <button
                type="button"
                className="chain-trace-turn-heading"
                aria-expanded={!collapsed}
                aria-controls={contentId}
                onClick={() => toggleTurn(turn.id)}
              >
                <ChevronDown size={14} aria-hidden="true" />
                <strong>{t('第 {count} 轮', { count: turn.ordinal })}</strong>
                <span className="chain-trace-turn-duration">
                  {durationLabel(Math.max(0, completedAt - startedAt), t)}
                </span>
              </button>
              {!collapsed && (
                <div id={contentId} className="chain-trace-turn-rows">
                  {nodes.map((node) => (
                    <TraceLedgerRow
                      key={node.id}
                      node={node}
                      selected={node.id === selectedId}
                      onSelect={onSelect}
                    />
                  ))}
                </div>
              )}
            </section>
          )
        })}
      </div>
      <OverlayScrollbar viewportRef={scrollRef} />
    </div>
  )
}

function TraceLedgerRow({
  node,
  selected,
  onSelect,
}: {
  node: TraceGraphNode
  selected: boolean
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  return (
    <button
      type="button"
      className={`chain-trace-ledger-row${selected ? ' is-selected' : ''}${node.failure ? ' has-error' : ''}`}
      data-trace-node-id={node.id}
      aria-label={`${traceNodeAccessibleLabel(node, t)}，${t('查看详情')}`}
      aria-current={selected || undefined}
      onClick={(event) => onSelect(node.id, event.currentTarget)}
    >
      <span className="chain-trace-ledger-rail"><i /></span>
      <TraceNodeType node={node} />
      <span className="chain-trace-ledger-main">
        <TraceNodeCopy node={node} showKind={false} />
      </span>
      <TraceNodeMeta node={node} />
    </button>
  )
}
