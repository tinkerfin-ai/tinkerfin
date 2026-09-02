import {
  Activity,
  ArrowLeft,
} from 'lucide-react'
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'

import type {
  TraceEntry,
  TraceEntryKind,
  TraceEntryStatus,
} from '../../../api/conversation/traceEntries'
import { Button, DrawerHeader, FilterPicker, FilterToggle } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { JsonObject } from '../../../types'
import { MarkdownContent } from '../components/MarkdownContent'
import {
  durationLabel,
  elapsedMilliseconds,
  traceKindLabel,
  traceStatusLabel,
} from './tracePresentation'
import { TraceTurnTree } from './TraceTurnTree'
import { useChainTrace } from './useChainTrace'

const SEMANTIC_KINDS: TraceEntryKind[] = [
  'agent',
  'model',
  'provider',
  'tool_proposal',
  'tool',
  'subagent',
  'skill',
  'memory',
  'guardrail',
  'retrieval',
  'custom',
]

const TECHNICAL_KINDS: TraceEntryKind[] = ['middleware', 'run', 'task', 'tools']
const DETAILS_INLINE_MIN_WIDTH = 800

const useTraceDetailsOverlay = (
  containerRef: RefObject<HTMLDivElement | null>,
  enabled: boolean,
) => {
  const [overlay, setOverlay] = useState(false)
  useLayoutEffect(() => {
    if (!enabled) return undefined
    const container = containerRef.current
    if (!container) return undefined
    const measure = () => setOverlay(container.clientWidth < DETAILS_INLINE_MIN_WIDTH)
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(container)
    return () => observer.disconnect()
  }, [containerRef, enabled])
  return overlay
}

const json = (value: unknown) => value == null ? '' : JSON.stringify(value, null, 2)

const isJsonObject = (value: unknown): value is JsonObject => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const messageText = (content: unknown) => {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return json(content)
  return content.map((block) => (
    isJsonObject(block) && block.type === 'text' && typeof block.text === 'string'
      ? block.text
      : json(block)
  )).filter(Boolean).join('\n')
}

const systemPrompt = (entry: TraceEntry) => {
  if (!entry.request || typeof entry.request !== 'object' || Array.isArray(entry.request)) return ''
  const messages = entry.request.messages
  if (!Array.isArray(messages)) return ''
  return messages
    .filter(isJsonObject)
    .filter((message) => message.messageType === 'system')
    .map((message) => messageText(message.content))
    .filter(Boolean)
    .join('\n\n')
}

type DetailTab = 'overview' | 'request' | 'system' | 'usage' | 'metadata' | 'timing' | 'result'

function TraceDetails({
  entry,
  focusClose,
  onClose,
}: {
  entry: TraceEntry
  focusClose: boolean
  onClose: () => void
}) {
  const { t } = useI18n()
  const [tab, setTab] = useState<DetailTab>('overview')
  const detailId = useId()
  const closeButton = useRef<HTMLButtonElement>(null)
  const priorFocusClose = useRef(false)
  const prompt = systemPrompt(entry)
  const tabs: Array<{ id: DetailTab; label: string }> = [
    { id: 'overview', label: t('概述') },
    ...(entry.request != null || entry.requestOmitted
      ? [{ id: 'request' as const, label: t('请求') }]
      : []),
    ...(prompt ? [{ id: 'system' as const, label: t('系统提示词') }] : []),
    ...(entry.usage != null ? [{ id: 'usage' as const, label: t('用量') }] : []),
    ...(entry.responseMetadata != null
      ? [{ id: 'metadata' as const, label: t('响应元数据') }]
      : []),
    { id: 'timing', label: t('计时') },
    ...(entry.result != null || entry.resultOmitted || entry.failure
      ? [{ id: 'result' as const, label: t('结果') }]
      : []),
  ]
  const activeTab = tabs.some((item) => item.id === tab) ? tab : 'overview'

  useLayoutEffect(() => {
    if (focusClose && !priorFocusClose.current) closeButton.current?.focus()
    priorFocusClose.current = focusClose
  }, [focusClose])

  const total = elapsedMilliseconds(entry)
  const ttft = entry.firstOutputAt
    ? Math.max(0, Date.parse(entry.firstOutputAt) - Date.parse(entry.startedAt))
    : null

  return (
    <aside id="chain-trace-details" className="chain-trace-details" aria-label={t('链路详情')}>
      <DrawerHeader
        ref={closeButton}
        className="chain-trace-details-header"
        title={entry.name}
        description={traceKindLabel(entry.kind, t)}
        closeLabel={t('关闭链路详情')}
        onClose={onClose}
      />
      <div className="chain-trace-detail-tabs" role="tablist" aria-label={t('链路详情分类')}>
        {tabs.map((item, index) => (
          <button
            type="button"
            role="tab"
            id={`${detailId}-${item.id}`}
            aria-controls={`${detailId}-panel`}
            aria-selected={activeTab === item.id}
            tabIndex={activeTab === item.id ? 0 : -1}
            className={activeTab === item.id ? 'is-active' : ''}
            key={item.id}
            onClick={() => setTab(item.id)}
            onKeyDown={(event) => {
              const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End']
              if (!keys.includes(event.key)) return
              event.preventDefault()
              const nextIndex = event.key === 'Home'
                ? 0
                : event.key === 'End'
                  ? tabs.length - 1
                  : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
              setTab(tabs[nextIndex].id)
              const buttons = event.currentTarget.parentElement
                ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
              buttons?.[nextIndex]?.focus()
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div
        id={`${detailId}-panel`}
        className="chain-trace-detail-body"
        role="tabpanel"
        aria-labelledby={`${detailId}-${activeTab}`}
      >
        {activeTab === 'overview' && (
          <dl className="chain-trace-summary">
            <div><dt>{t('状态')}</dt><dd>{traceStatusLabel(entry.status, t)}</dd></div>
            <div><dt>{t('类型')}</dt><dd>{traceKindLabel(entry.kind, t)}</dd></div>
            <div><dt>{t('节点 ID')}</dt><dd>{entry.id}</dd></div>
            <div><dt>{t('运行 ID')}</dt><dd>{entry.runId}</dd></div>
            <div><dt>{t('图范围')}</dt><dd>{entry.namespace.length ? entry.namespace.join(' / ') : t('根图')}</dd></div>
            {entry.provider && <div><dt>{t('提供方')}</dt><dd>{entry.provider}</dd></div>}
            {entry.model && <div><dt>{t('模型')}</dt><dd>{entry.model}</dd></div>}
            {entry.agentName && <div><dt>{t('智能体')}</dt><dd>{entry.agentName}</dd></div>}
            {entry.sourceId && <div><dt>{t('来源 ID')}</dt><dd>{entry.sourceId}</dd></div>}
            {entry.proposalId && <div><dt>{t('提议 ID')}</dt><dd>{entry.proposalId}</dd></div>}
            {entry.sourcePath && <div><dt>{t('来源')}</dt><dd>{entry.sourcePath}</dd></div>}
            {entry.className && <div><dt>{t('实现')}</dt><dd>{entry.className}</dd></div>}
            {entry.hooks.length > 0 && <div><dt>{t('调用钩子')}</dt><dd>{entry.hooks.join(', ')}</dd></div>}
          </dl>
        )}
        {activeTab === 'request' && <pre>{entry.requestOmitted ? t('请求内容未保留') : json(entry.request)}</pre>}
        {activeTab === 'system' && <MarkdownContent content={prompt} />}
        {activeTab === 'usage' && <pre>{json(entry.usage)}</pre>}
        {activeTab === 'metadata' && <pre>{json(entry.responseMetadata)}</pre>}
        {activeTab === 'timing' && (
          <dl className="chain-trace-summary">
            <div><dt>{t('开始时间')}</dt><dd>{new Date(entry.startedAt).toLocaleString()}</dd></div>
            <div><dt>{t('总时长')}</dt><dd>{durationLabel(total, t)}</dd></div>
            <div><dt>{t('首 token 延迟')}</dt><dd>{ttft == null ? t('不可用') : durationLabel(ttft, t)}</dd></div>
          </dl>
        )}
        {activeTab === 'result' && (
          <pre>{entry.failure
            ? json({
                errorType: entry.failure.errorType,
                message: entry.failure.message ?? null,
                code: entry.failure.code ?? null,
              })
            : entry.resultOmitted ? t('结果内容未保留') : json(entry.result)}</pre>
        )}
      </div>
    </aside>
  )
}

export function ChainTraceView({
  threadId,
  active,
  headerTarget,
  onReturnToConversation,
}: {
  threadId: string
  active: boolean
  headerTarget: HTMLElement | null
  onReturnToConversation: () => void
}) {
  const { t } = useI18n()
  const [kind, setKind] = useState<TraceEntryKind | 'all'>('all')
  const [status, setStatus] = useState<TraceEntryStatus | 'all'>('all')
  const [showTechnical, setShowTechnical] = useState(false)
  const [selectedId, setSelectedId] = useState<string>()
  const detailTrigger = useRef<HTMLButtonElement | null>(null)
  const returnButton = useRef<HTMLButtonElement | null>(null)
  const splitRef = useRef<HTMLDivElement>(null)
  const traceRegion = useRef<HTMLElement>(null)
  const filter = useMemo(() => ({
    kinds: kind === 'all'
      ? [...SEMANTIC_KINDS, ...(showTechnical ? TECHNICAL_KINDS : [])]
      : [kind],
    statuses: status === 'all' ? undefined : [status],
    includeAncestors: true,
  }), [kind, showTechnical, status])
  const trace = useChainTrace({ threadId, active, filter })
  const page = trace.state.phase === 'ready' ? trace.state.page : undefined
  const detailsOverlay = useTraceDetailsOverlay(splitRef, Boolean(page?.items.length))
  const entriesById = useMemo(
    () => new Map(page?.items.map((entry) => [entry.id, entry]) ?? []),
    [page?.items],
  )
  const selected = selectedId ? entriesById.get(selectedId) : undefined
  useEffect(() => {
    if (selectedId && !entriesById.has(selectedId)) {
      detailTrigger.current = null
      setSelectedId(undefined)
      window.requestAnimationFrame(() => {
        traceRegion.current?.querySelector<HTMLElement>('.chain-trace-tree-scroll')?.focus()
      })
    }
  }, [entriesById, selectedId])

  const selectEntry = (entryId: string, trigger: HTMLButtonElement) => {
    detailTrigger.current = trigger
    setSelectedId(entryId)
  }

  const closeDetails = () => {
    setSelectedId(undefined)
    window.requestAnimationFrame(() => detailTrigger.current?.focus())
  }

  useEffect(() => {
    if (!selectedId) return undefined
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setSelectedId(undefined)
      window.requestAnimationFrame(() => detailTrigger.current?.focus())
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [selectedId])

  useEffect(() => {
    if (!headerTarget) return undefined
    const frame = window.requestAnimationFrame(() => returnButton.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [headerTarget])

  const kindOptions: readonly (TraceEntryKind | 'all')[] = [
    'all',
    ...SEMANTIC_KINDS,
    ...(showTechnical ? TECHNICAL_KINDS : []),
  ]
  const statusOptions: readonly (TraceEntryStatus | 'all')[] = [
    'all',
    ...(Object.keys(page?.facets.statuses ?? {}) as TraceEntryStatus[]),
  ]

  const headerControls = (
    <>
      <button ref={returnButton} type="button" className="chain-trace-back" onClick={onReturnToConversation}>
        <ArrowLeft size={15} aria-hidden="true" />
        <span>{t('返回对话')}</span>
      </button>
      {threadId && (
        <>
          <FilterPicker
            value={kind}
            options={kindOptions}
            label={t('类型')}
            renderLabel={(value) => value === 'all'
              ? t('全部类型')
              : `${traceKindLabel(value, t)} · ${page?.facets.kinds[value] ?? 0}`}
            onChange={setKind}
          />
          <FilterPicker
            value={status}
            options={statusOptions}
            label={t('状态')}
            renderLabel={(value) => value === 'all'
              ? t('全部状态')
              : `${traceStatusLabel(value, t)} · ${page?.facets.statuses[value] ?? 0}`}
            onChange={setStatus}
          />
          <FilterToggle
            pressed={showTechnical}
            label={t('技术节点')}
            onPressedChange={(visible) => {
              setShowTechnical(visible)
              if (!visible && TECHNICAL_KINDS.includes(kind as TraceEntryKind)) {
                setKind('all')
              }
            }}
          />
        </>
      )}
    </>
  )
  const headerPortal = headerTarget ? createPortal(headerControls, headerTarget) : null

  if (!threadId) {
    return (
      <>
        {headerPortal}
        <section ref={traceRegion} className="chain-trace" aria-label={t('链路')}>
          <div className="chain-trace-empty"><Activity size={28} /><h2>{t('链路')}</h2><p>{t('发起会话后可查看真实调用链')}</p></div>
        </section>
      </>
    )
  }

  const entries = page?.items ?? []
  const forceExpandTree = kind !== 'all' || status !== 'all'

  return (
    <>
      {headerPortal}
      <section ref={traceRegion} className="chain-trace" aria-label={t('链路')}>
        {trace.state.phase === 'loading' && <div className="chain-trace-state" role="status">{t('正在加载链路…')}</div>}
        {trace.state.phase === 'error' && (
          <div className="chain-trace-state is-error" role="alert">
            <p>{t('链路加载失败')}</p>
            <Button onClick={trace.retry}>{t('重试')}</Button>
          </div>
        )}
        {(page?.completeness.executionTreeMissing || page?.completeness.callTrackingMissing) && (
          <div className="chain-trace-notice">
            {page.completeness.executionTreeMissing
              ? t('部分历史运行没有完整执行树')
              : t('部分历史运行没有调用级跟踪数据')}
          </div>
        )}
        {trace.state.phase === 'ready' && entries.length === 0 && (
          <div className="chain-trace-state"><Activity size={24} /><p>{t('没有匹配的链路节点')}</p></div>
        )}
        {trace.state.phase === 'ready' && entries.length > 0 && (
          <div
            ref={splitRef}
            className={`chain-trace-split${selected ? ' has-details' : ''}${detailsOverlay ? ' uses-overlay' : ''}`}
          >
            <TraceTurnTree
              turns={page?.turns ?? []}
              entries={entries}
              selectedId={selectedId}
              forceExpandAll={forceExpandTree}
              forceExpandKey={forceExpandTree ? `${kind}:${status}` : undefined}
              hiddenKinds={showTechnical ? [] : TECHNICAL_KINDS}
              backgroundInert={detailsOverlay && Boolean(selected)}
              onSelect={selectEntry}
            />
            {selected && (
              <TraceDetails
                key={selected.id}
                entry={selected}
                focusClose={detailsOverlay}
                onClose={closeDetails}
              />
            )}
          </div>
        )}
      </section>
    </>
  )
}
