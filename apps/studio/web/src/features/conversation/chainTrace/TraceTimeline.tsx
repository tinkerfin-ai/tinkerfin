import {
  Cpu,
  FolderOpen,
  Link2,
  MessageCircleMore,
  WandSparkles,
} from 'lucide-react'
import { useRef } from 'react'
import type { CSSProperties } from 'react'

import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { OverlayScrollbar } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { durationLabel, traceNodeAccessibleLabel } from './tracePresentation'
import type {
  TraceTimelineLane,
  TraceTimelineLaneId,
  TraceTimelineLayout,
} from './traceLayout'

interface TimelineBarStyle extends CSSProperties {
  '--chain-trace-bar-left': string
  '--chain-trace-bar-width': string
  '--chain-trace-bar-track': number
}

const laneIcon = (lane: TraceTimelineLaneId) => ({
  user: <MessageCircleMore size={14} aria-hidden="true" />,
  assistant: <WandSparkles size={14} aria-hidden="true" />,
  tool: <Link2 size={14} aria-hidden="true" />,
  context: <FolderOpen size={14} aria-hidden="true" />,
  technical: <Cpu size={14} aria-hidden="true" />,
}[lane])

const laneLabel = (lane: TraceTimelineLaneId, t: ReturnType<typeof useI18n>['t']) => ({
  user: t('用户'),
  assistant: t('助手'),
  tool: t('工具'),
  context: t('上下文'),
  technical: t('技术'),
}[lane])

const laneHeight = (lane: TraceTimelineLane) => 17 + Math.max(0, lane.trackCount - 1) * 10

export function TraceTimeline({
  layout,
  selected,
  backgroundInert,
  onSelect,
}: {
  layout: TraceTimelineLayout
  selected?: TraceGraphNode
  backgroundInert: boolean
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const scrollRef = useRef<HTMLDivElement>(null)
  const selectedBar = selected
    ? layout.lanes.flatMap((lane) => lane.bars).find((bar) => bar.node.id === selected.id)
    : undefined
  const rowTemplate = `16px ${layout.lanes.map(laneHeight).map((height) => `${height}px`).join(' ')}`

  return (
    <section
      className="chain-trace-timeline"
      aria-label={t('调用时间线')}
      aria-hidden={backgroundInert || undefined}
      inert={backgroundInert || undefined}
    >
      <div className="chain-trace-timeline-labels" style={{ gridTemplateRows: rowTemplate }}>
        <span aria-hidden="true" />
        {layout.lanes.map((lane) => (
          <span key={lane.id} className={`chain-trace-lane-label is-${lane.id}`}>
            {laneIcon(lane.id)}
            {laneLabel(lane.id, t)}
          </span>
        ))}
      </div>
      <div className="chain-trace-timeline-scroll-host">
        <div
          ref={scrollRef}
          className="chain-trace-timeline-scroll"
          role="region"
          aria-label={t('时间线图表')}
          tabIndex={0}
        >
          <div className="chain-trace-timeline-chart" style={{ gridTemplateRows: rowTemplate }}>
            <div className="chain-trace-ticks">
              {layout.ticks.map((tick, index) => (
                <span key={tick} style={{ left: `${index / (layout.ticks.length - 1) * 100}%` }}>
                  {durationLabel(tick, t)}
                </span>
              ))}
            </div>
            {selectedBar && (
              <div
                className="chain-trace-timeline-selection"
                style={{
                  left: `${selectedBar.leftPercent}%`,
                  width: `${Math.max(selectedBar.widthPercent, .35)}%`,
                }}
                aria-hidden="true"
              />
            )}
            {layout.lanes.map((lane) => (
              <div key={lane.id} className={`chain-trace-lane is-${lane.id}`}>
                {lane.bars.map((bar) => (
                  <button
                    key={bar.node.id}
                    type="button"
                    className={`chain-trace-timeline-bar${bar.node.id === selected?.id ? ' is-selected' : ''}${bar.node.failure ? ' has-error' : ''}`}
                    style={{
                      '--chain-trace-bar-left': `${bar.leftPercent}%`,
                      '--chain-trace-bar-width': `${bar.widthPercent}%`,
                      '--chain-trace-bar-track': bar.track,
                    } as TimelineBarStyle}
                    aria-label={t('选择 {name}，{duration}', {
                      name: traceNodeAccessibleLabel(bar.node, t),
                      duration: durationLabel(
                        Math.max(0, bar.endMilliseconds - bar.startMilliseconds),
                        t,
                      ),
                    })}
                    onClick={(event) => onSelect(bar.node.id, event.currentTarget)}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>
        <OverlayScrollbar viewportRef={scrollRef} axis="horizontal" size="compact" />
      </div>
    </section>
  )
}
