import { Activity, Search, ShieldAlert, X } from 'lucide-react'
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react'

import type {
  TraceGraphNode,
  TraceGraphNodeKind,
} from '../../../api/conversation/traceGraph'
import {
  Button,
  DrawerHeader,
  FeedbackState,
  FilterToggle,
  IconButton,
  OverlayScrollbar,
  ViewTabs,
} from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { JsonObject, JsonValue } from '../../../types'
import { MarkdownContent } from '../components/MarkdownContent'
import { TraceCategoryFilters } from './TraceCategoryFilters'
import { TraceLedger } from './TraceLedger'
import { TraceNodeType } from './TraceNodeVisual'
import { TraceTimeline } from './TraceTimeline'
import { TraceTurnTree } from './TraceTurnTree'
import {
  buildTraceTimelineLayout,
  groupTraceNodesByTurn,
  preferredTraceNode,
} from './traceLayout'
import {
  durationLabel,
  elapsedMilliseconds,
  TRACE_PUBLIC_CATEGORIES,
  TRACE_PUBLIC_CATEGORY_KINDS,
  TRACE_TECHNICAL_KINDS,
  traceKindLabel,
  traceStatusLabel,
  type TracePublicCategory,
} from './tracePresentation'
import { useChainTrace } from './useChainTrace'
import { useTraceModelResponse } from './useTraceModelResponse'

const TRACE_VIEW_STORAGE_KEY = 'tinkerfin.chain-trace.view'
const TRACE_SEARCH_DELAY_MS = 250
const DETAILS_INLINE_MIN_WIDTH = 920

type TraceViewMode = 'timeline' | 'tree'
type DetailTab = 'overview' | 'request' | 'system' | 'response' | 'usage' | 'timing' | 'result'

const readTraceView = (): TraceViewMode => {
  try {
    return window.localStorage.getItem(TRACE_VIEW_STORAGE_KEY) === 'tree'
      ? 'tree'
      : 'timeline'
  } catch {
    return 'timeline'
  }
}

const saveTraceView = (value: TraceViewMode) => {
  try {
    window.localStorage.setItem(TRACE_VIEW_STORAGE_KEY, value)
  } catch {
    // 浏览器禁用持久化时仍保留当前页面内的选择
  }
}

const json = (value: unknown) => value == null ? '' : JSON.stringify(value, null, 2)

const isJsonObject = (value: unknown): value is JsonObject => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

interface UsageRow {
  key: string
  path: string[]
  value: string | number | boolean | null
}

const flattenUsage = (
  value: JsonValue,
  path: string[] = [],
): UsageRow[] => {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => flattenUsage(item, [...path, String(index + 1)]))
  }
  if (isJsonObject(value)) {
    return Object.entries(value).flatMap(([name, item]) => (
      flattenUsage(item, [...path, name])
    ))
  }
  return [{ key: path.join('.'), path, value }]
}

const mergeTraceNodes = (
  ...groups: ReadonlyArray<readonly TraceGraphNode[]>
) => {
  const byId = new Map<string, TraceGraphNode>()
  groups.flat().forEach((node) => {
    const current = byId.get(node.id)
    if (!current || node.updatedSeq >= current.updatedSeq) byId.set(node.id, node)
  })
  return [...byId.values()].sort((left, right) => (
    left.startedSeq - right.startedSeq || left.id.localeCompare(right.id)
  ))
}

const findTraceNodeTrigger = (
  region: HTMLElement | null,
  nodeId: string | undefined,
) => {
  if (!nodeId) return undefined
  return [...(region?.querySelectorAll<HTMLButtonElement>('[data-trace-node-id]') ?? [])]
    .find((element) => element.dataset.traceNodeId === nodeId)
}

const restoreTraceNodeFocus = (
  region: HTMLElement | null,
  nodeId: string | undefined,
  trigger: HTMLButtonElement | null,
) => {
  window.requestAnimationFrame(() => {
    const target = trigger?.isConnected
      ? trigger
      : findTraceNodeTrigger(region, nodeId)
    target?.focus()
  })
}

const messageText = (content: unknown) => {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return json(content)
  return content.map((block) => (
    isJsonObject(block) && block.type === 'text' && typeof block.text === 'string'
      ? block.text
      : json(block)
  )).filter(Boolean).join('\n')
}

const systemPrompt = (entry: TraceGraphNode) => {
  if (entry.kind === 'system_message') return messageText(entry.content)
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

const useTraceDetailsOverlay = (
  containerRef: RefObject<HTMLDivElement | null>,
  enabled: boolean,
) => {
  const [overlay, setOverlay] = useState(false)
  useLayoutEffect(() => {
    if (!enabled) return undefined
    const container = containerRef.current
    if (!container) return undefined
    const measure = () => {
      if (container.clientWidth > 0) {
        setOverlay(container.clientWidth <= DETAILS_INLINE_MIN_WIDTH)
      }
    }
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

const useTraceNow = (nodes: readonly TraceGraphNode[], active: boolean) => {
  const [now, setNow] = useState(() => Date.now())
  const running = active && nodes.some((node) => (
    !node.completedAt && (node.status === 'running' || node.status === 'waiting')
  ))
  useEffect(() => {
    setNow(Date.now())
    if (!running) return undefined
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  return now
}

function TraceDetails({
  entry,
  responseEntries,
  responseStatus,
  turnOrdinal,
  stepOrdinal,
  focusClose,
  onRetryResponse,
  onClose,
}: {
  entry: TraceGraphNode
  responseEntries: TraceGraphNode[]
  responseStatus: 'loading' | 'ready' | 'error'
  turnOrdinal?: number
  stepOrdinal?: number
  focusClose: boolean
  onRetryResponse: () => void
  onClose: () => void
}) {
  const { locale, t } = useI18n()
  const [tab, setTab] = useState<DetailTab>('overview')
  const closeButton = useRef<HTMLButtonElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const priorFocusClose = useRef(false)
  const prompt = systemPrompt(entry)
  const responseMessages = responseEntries
    .filter((item) => item.kind === 'assistant_message')
    .map((item) => ({
      id: item.sourceId ?? item.id,
      type: 'AIMessage',
      content: messageText(item.content),
    }))
  const responseTools = responseEntries
    .filter((item) => item.kind === 'tool')
    .map((item) => ({
      id: item.sourceId ?? item.id,
      name: item.name,
      arguments: item.request ?? null,
    }))
  const responseData = {
    messages: responseMessages.map(({ id, type }) => ({ id, type })),
    toolCalls: responseTools,
    usageMetadata: entry.usage ?? null,
    responseMetadata: entry.responseMetadata ?? null,
  }
  const messageContent = entry.kind.endsWith('_message')
    ? messageText(entry.content)
    : ''
  const usageRows = entry.usage == null ? [] : flattenUsage(entry.usage)
  const usageLabels: Record<string, string> = {
    input_tokens: t('输入 Tokens'),
    output_tokens: t('输出 Tokens'),
    total_tokens: t('总 Tokens'),
    'input_token_details.cache_read': t('缓存读取 Tokens'),
    'input_token_details.cache_creation': t('缓存创建 Tokens'),
    'input_token_details.audio': t('输入音频 Tokens'),
    'output_token_details.reasoning': t('推理 Tokens'),
    'output_token_details.audio': t('输出音频 Tokens'),
    'output_token_details.accepted_prediction': t('接受预测 Tokens'),
    'output_token_details.rejected_prediction': t('拒绝预测 Tokens'),
  }
  const tabs: Array<{ id: DetailTab; label: string }> = [
    { id: 'overview', label: t('概述') },
    ...(entry.request != null || entry.requestOmitted
      ? [{ id: 'request' as const, label: t('请求') }]
      : []),
    ...(prompt ? [{ id: 'system' as const, label: t('系统提示词') }] : []),
    ...(entry.kind === 'model'
      ? [{ id: 'response' as const, label: t('响应') }]
      : []),
    ...(usageRows.length > 0 ? [{ id: 'usage' as const, label: t('用量') }] : []),
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
  const positionLabel = turnOrdinal != null && stepOrdinal != null
    ? t('第 {turn} 轮 · 步骤 {step}', { turn: turnOrdinal, step: stepOrdinal })
    : durationLabel(total, t)

  return (
    <aside id="chain-trace-details" className="chain-trace-details" aria-label={t('链路详情')}>
      <DrawerHeader
        ref={closeButton}
        density="compact"
        className="chain-trace-details-header"
        title={(
          <span className="chain-trace-details-context">
            <TraceNodeType node={entry} />
            <span>{positionLabel}</span>
          </span>
        )}
        closeLabel={t('关闭链路详情')}
        onClose={onClose}
      />
      <ViewTabs
        value={activeTab}
        options={tabs.map((item) => ({
          value: item.id,
          label: item.label,
          controls: 'chain-trace-detail-panel',
        }))}
        label={t('链路详情分类')}
        density="medium"
        className="chain-trace-detail-tabs"
        onChange={setTab}
      />
      <div className="chain-trace-detail-body-host">
        <div
          ref={bodyRef}
          id="chain-trace-detail-panel"
          className="chain-trace-detail-body"
          role="tabpanel"
          aria-label={tabs.find((item) => item.id === activeTab)?.label}
        >
          {activeTab === 'overview' && (
            <>
              {entry.failure && (
                <section className="chain-trace-error-panel" aria-label={t('错误详情')}>
                  <ShieldAlert size={20} aria-hidden="true" />
                  <div>
                    <strong>{entry.failure.errorType}</strong>
                    {entry.failure.message && <p>{entry.failure.message}</p>}
                    {(entry.request != null || entry.requestOmitted) && (
                      <Button size="xs" onClick={() => setTab('request')}>{t('查看请求')}</Button>
                    )}
                  </div>
                </section>
              )}
              {messageContent && (
                <div className="chain-trace-detail-message">
                  <MarkdownContent content={messageContent} variant="compact" />
                </div>
              )}
              <h3 className="chain-trace-section-title">{t('基本信息')}</h3>
              <dl className="chain-trace-summary">
                <div><dt>{t('状态')}</dt><dd>{traceStatusLabel(entry.status, t)}</dd></div>
                <div><dt>{t('类型')}</dt><dd>{traceKindLabel(entry.kind, t)}</dd></div>
                <div><dt>{t('节点 ID')}</dt><dd>{entry.id}</dd></div>
                <div><dt>{t('运行 ID')}</dt><dd>{entry.runId}</dd></div>
                <div><dt>{t('图范围')}</dt><dd>{entry.namespace.length ? entry.namespace.join(' / ') : t('根图')}</dd></div>
                {entry.provider && <div><dt>{t('提供方')}</dt><dd>{entry.provider}</dd></div>}
                {entry.model && <div><dt>{t('模型')}</dt><dd>{entry.model}</dd></div>}
                {entry.kind === 'tool' && <div><dt>{t('工具')}</dt><dd>{entry.name}</dd></div>}
                {entry.agentName && <div><dt>{t('智能体')}</dt><dd>{entry.agentName}</dd></div>}
                {entry.sourceId && <div><dt>{t('来源 ID')}</dt><dd>{entry.sourceId}</dd></div>}
                {entry.sourcePath && <div><dt>{t('来源')}</dt><dd>{entry.sourcePath}</dd></div>}
                {entry.className && <div><dt>{t('实现')}</dt><dd>{entry.className}</dd></div>}
                {entry.hooks.length > 0 && <div><dt>{t('调用钩子')}</dt><dd>{entry.hooks.join(', ')}</dd></div>}
              </dl>
            </>
          )}
          {activeTab === 'request' && <pre>{entry.requestOmitted ? t('请求内容未保留') : json(entry.request)}</pre>}
          {activeTab === 'system' && <MarkdownContent content={prompt} variant="compact" />}
          {activeTab === 'response' && (
            <div className="chain-trace-response">
              {responseStatus === 'loading' ? (
                <div className="chain-trace-response-state" role="status">
                  {t('正在加载完整响应…')}
                </div>
              ) : responseStatus === 'error' ? (
                <div className="chain-trace-response-state is-error" role="alert">
                  <p>{t('完整响应加载失败')}</p>
                  <Button size="xs" onClick={onRetryResponse}>{t('重试')}</Button>
                </div>
              ) : (
                <>
                  {responseMessages.map((message) => message.content && (
                    <section key={message.id} className="chain-trace-response-message">
                      <span>{message.type} · {message.id}</span>
                      <MarkdownContent content={message.content} variant="compact" />
                    </section>
                  ))}
                  <section className="chain-trace-response-data">
                    <h3>{t('响应数据')}</h3>
                    <pre>{json(responseData)}</pre>
                  </section>
                </>
              )}
            </div>
          )}
          {activeTab === 'usage' && (
            <section className="chain-trace-usage">
              <h3 className="chain-trace-section-title">{t('Token 用量')}</h3>
              <dl className="chain-trace-summary">
                {usageRows.map(({ key, path, value }) => (
                  <div key={key || 'usage'}>
                    <dt>{(usageLabels[key] ?? path.join(' / ')) || t('用量')}</dt>
                    <dd>{typeof value === 'number'
                      ? new Intl.NumberFormat(locale).format(value)
                      : value == null ? t('不可用') : String(value)}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
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
        <OverlayScrollbar viewportRef={bodyRef} />
      </div>
    </aside>
  )
}

export function ChainTraceView({
  threadId,
  active,
}: {
  threadId: string
  active: boolean
}) {
  const { locale, t } = useI18n()
  const [view, setView] = useState<TraceViewMode>(readTraceView)
  const [categories, setCategories] = useState<ReadonlySet<TracePublicCategory>>(
    () => new Set(TRACE_PUBLIC_CATEGORIES),
  )
  const [showTechnical, setShowTechnical] = useState(false)
  const [isSearchOpen, setSearchOpen] = useState(false)
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string>()
  const detailTrigger = useRef<HTMLButtonElement | null>(null)
  const manualClose = useRef(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const traceRegion = useRef<HTMLElement>(null)
  const filtersRef = useRef<HTMLDivElement>(null)
  const searchControlRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchTriggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const timer = window.setTimeout(
      () => setSearchQuery(searchInput.trim()),
      TRACE_SEARCH_DELAY_MS,
    )
    return () => window.clearTimeout(timer)
  }, [searchInput])

  useEffect(() => {
    if (!isSearchOpen) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && searchControlRef.current?.contains(target)) return
      if (searchInput.trim()) searchInputRef.current?.blur()
      else setSearchOpen(false)
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    return () => document.removeEventListener('pointerdown', handleOutsidePointerDown)
  }, [isSearchOpen, searchInput])

  const categoryKey = TRACE_PUBLIC_CATEGORIES
    .filter((category) => categories.has(category))
    .join(':')
  const requestedKinds = useMemo<TraceGraphNodeKind[]>(() => [
    ...TRACE_PUBLIC_CATEGORIES.flatMap((category) => (
      categories.has(category) ? TRACE_PUBLIC_CATEGORY_KINDS[category] : []
    )),
    ...(showTechnical ? TRACE_TECHNICAL_KINDS : []),
  ], [categories, showTechnical])
  const filter = useMemo(() => ({
    kinds: requestedKinds,
    query: searchQuery || undefined,
    includeTechnicalNodes: showTechnical,
    includeAncestorNodes: true,
  }), [requestedKinds, searchQuery, showTechnical])
  const trace = useChainTrace({ threadId, active, filter, limit: 1000 })
  const page = trace.state.phase === 'ready' ? trace.state.page : undefined
  const graphNodes = useMemo(() => page?.nodes ?? [], [page?.nodes])
  const matchedNodeIds = useMemo(
    () => new Set(page?.matchedNodeIds ?? []),
    [page?.matchedNodeIds],
  )
  const nodes = useMemo(
    () => graphNodes.filter((node) => matchedNodeIds.has(node.id)),
    [graphNodes, matchedNodeIds],
  )
  const visibleNodeIds = useMemo(
    () => new Set(nodes.map((node) => node.id)),
    [nodes],
  )
  const now = useTraceNow(nodes, active)
  const timeline = useMemo(
    () => buildTraceTimelineLayout(nodes, now),
    [nodes, now],
  )
  const turnRows = useMemo(
    () => groupTraceNodesByTurn(page?.turns ?? [], nodes),
    [nodes, page?.turns],
  )
  const nodesById = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  )
  const selected = selectedId ? nodesById.get(selectedId) : undefined
  const selectedPosition = useMemo(() => {
    if (!selected) return undefined
    for (const { turn, nodes: turnNodes } of turnRows) {
      const index = turnNodes.findIndex((node) => node.id === selected.id)
      if (index >= 0) return { turnOrdinal: turn.ordinal, stepOrdinal: index + 1 }
    }
    return undefined
  }, [selected, turnRows])
  const localResponseEntries = useMemo(() => nodes.filter((node) => (
    node.parentId === selected?.id
    && (node.kind === 'assistant_message' || node.kind === 'tool')
  )), [nodes, selected?.id])
  const mainResponseEntriesComplete = categories.has('assistant')
    && categories.has('tool')
    && !searchQuery
    && !page?.completeness.detailsOmitted
  const responseQueryEnabled = selected?.kind === 'model'
    && !mainResponseEntriesComplete
  const responseRevision = selected?.kind === 'model'
    ? [
        page?.asOfSeq ?? 0,
        selected.updatedSeq,
        ...localResponseEntries.map((entry) => `${entry.id}:${entry.updatedSeq}`),
      ].join('|')
    : ''
  const modelResponse = useTraceModelResponse({
    threadId,
    modelId: selected?.kind === 'model' ? selected.id : undefined,
    enabled: responseQueryEnabled,
    responseRevision,
  })
  const queriedResponseMatches = modelResponse.state.phase !== 'idle'
    && modelResponse.state.modelId === selected?.id
    && modelResponse.state.responseRevision === responseRevision
  const responseStatus = responseQueryEnabled
    ? queriedResponseMatches ? modelResponse.state.phase : 'loading'
    : 'ready'
  const responseEntries = responseQueryEnabled
    && queriedResponseMatches
    && modelResponse.state.phase === 'ready'
    ? mergeTraceNodes(localResponseEntries, modelResponse.state.entries)
    : responseQueryEnabled ? [] : localResponseEntries
  const detailsOverlay = useTraceDetailsOverlay(contentRef, nodes.length > 0)
  const incomplete = page?.nextCursor != null
  const completenessMessage = page?.completeness.relationshipEvidenceMissing
    ? t('部分节点缺少完整关联依据')
    : page?.completeness.callTrackingMissing
      ? t('部分历史运行没有调用级跟踪数据')
      : undefined

  useEffect(() => {
    manualClose.current = false
    detailTrigger.current = null
    setSelectedId(undefined)
  }, [threadId])

  useEffect(() => {
    if (!page || incomplete || nodes.length === 0) return
    if (selectedId && nodesById.has(selectedId)) return
    if (manualClose.current && !selectedId) return
    detailTrigger.current = null
    setSelectedId(preferredTraceNode(nodes)?.id)
  }, [incomplete, nodes, nodesById, page, selectedId])

  useEffect(() => {
    if (!selectedId) return undefined
    const closeOnEscape = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented
        || event.key !== 'Escape'
        || (event.target instanceof Element && event.target.closest('[role="listbox"]'))
      ) return
      event.preventDefault()
      const trigger = detailTrigger.current
      const nodeId = selectedId
      manualClose.current = true
      setSelectedId(undefined)
      restoreTraceNodeFocus(traceRegion.current, nodeId, trigger)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [selectedId])

  const locateNode = (nodeId: string) => {
    window.requestAnimationFrame(() => {
      const target = findTraceNodeTrigger(traceRegion.current, nodeId)
      target?.scrollIntoView?.({
        block: 'center',
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'auto'
          : 'smooth',
      })
    })
  }

  const selectEntry = (
    entryId: string,
    trigger: HTMLButtonElement,
    locate = false,
  ) => {
    manualClose.current = false
    detailTrigger.current = trigger
    setSelectedId(entryId)
    if (locate) locateNode(entryId)
  }

  const closeDetails = () => {
    const trigger = detailTrigger.current
    const nodeId = selectedId
    manualClose.current = true
    setSelectedId(undefined)
    restoreTraceNodeFocus(traceRegion.current, nodeId, trigger)
  }

  const changeView = (next: TraceViewMode) => {
    setView(next)
    saveTraceView(next)
    if (selectedId) locateNode(selectedId)
  }

  const toggleCategory = (category: TracePublicCategory) => {
    setCategories((current) => {
      if (current.has(category) && current.size === 1) return current
      const next = new Set(current)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })
  }

  const openSearch = () => {
    setSearchOpen(true)
    window.requestAnimationFrame(() => searchInputRef.current?.focus())
  }

  const clearAndCloseSearch = () => {
    setSearchInput('')
    setSearchQuery('')
    setSearchOpen(false)
    window.requestAnimationFrame(() => searchTriggerRef.current?.focus())
  }

  if (!threadId) {
    return (
      <section ref={traceRegion} className="chain-trace" role="tabpanel" aria-label={t('链路')}>
        <div className="chain-trace-empty"><Activity size={28} /><h2>{t('链路')}</h2><p>{t('发起会话后可查看真实调用链')}</p></div>
      </section>
    )
  }

  return (
    <section ref={traceRegion} id="chain-trace-panel" className="chain-trace" role="tabpanel" aria-label={t('链路')}>
      <div
        className="chain-trace-toolbar-host"
        aria-hidden={detailsOverlay && Boolean(selected) || undefined}
        inert={detailsOverlay && Boolean(selected) || undefined}
      >
        <div className="chain-trace-toolbar" aria-label={t('链路筛选')}>
          <div className="chain-trace-toolbar-context">
            <div className="chain-trace-range-summary" aria-live="polite">
              <span>{t('当前范围')} <strong>{durationLabel(timeline?.durationMilliseconds ?? 0, t)}</strong></span>
              <i aria-hidden="true">/</i>
              <span>
                <strong>{page?.turns.length ?? 0}</strong>{' '}
                {t('轮')}{locale === 'en' && page?.turns.length !== 1 ? 's' : ''}
              </span>
              <i aria-hidden="true">/</i>
              <span><strong>{nodes.length}</strong> {t('节点')}</span>
            </div>
            {completenessMessage && (
              <span className="chain-trace-completeness" role="status">
                {completenessMessage}
              </span>
            )}
          </div>
          <div className="chain-trace-filters-host">
            <div ref={filtersRef} className="chain-trace-filters">
              <ViewTabs
                value={view}
                density="compact"
                label={t('链路布局')}
                options={[
                  { value: 'timeline', label: t('时间线'), controls: 'chain-trace-timeline-panel' },
                  { value: 'tree', label: t('树形'), controls: 'chain-trace-tree-panel' },
                ]}
                onChange={changeView}
              />
              <TraceCategoryFilters selected={categories} onToggle={toggleCategory} />
              <FilterToggle
                pressed={showTechnical}
                label={t('技术')}
                onPressedChange={setShowTechnical}
              />
              <div
                ref={searchControlRef}
                className={`chain-trace-search-control${isSearchOpen ? ' is-open' : ''}`}
              >
                <IconButton
                  ref={searchTriggerRef}
                  className="chain-trace-search-trigger"
                  size="sm"
                  label={t('搜索链路节点')}
                  tooltip={t('搜索链路节点')}
                  icon={<Search size={16} />}
                  selected={isSearchOpen || Boolean(searchInput)}
                  tabIndex={isSearchOpen ? -1 : 0}
                  aria-expanded={isSearchOpen}
                  aria-controls="chain-trace-search"
                  onClick={openSearch}
                />
                <label
                  id="chain-trace-search"
                  className="chain-trace-search"
                  aria-hidden={!isSearchOpen || undefined}
                  inert={!isSearchOpen || undefined}
                >
                  <Search size={14} aria-hidden="true" />
                  <input
                    ref={searchInputRef}
                    type="text"
                    role="searchbox"
                    aria-label={t('搜索链路节点')}
                    value={searchInput}
                    placeholder={t('搜索节点、内容')}
                    onChange={(event) => setSearchInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key !== 'Escape') return
                      event.preventDefault()
                      clearAndCloseSearch()
                    }}
                  />
                  <IconButton
                    size="xs"
                    label={t('清除链路搜索')}
                    icon={<X size={13} />}
                    onClick={clearAndCloseSearch}
                  />
                </label>
              </div>
            </div>
            <OverlayScrollbar viewportRef={filtersRef} axis="horizontal" size="compact" />
          </div>
        </div>
      </div>

      {trace.state.phase === 'loading' && (
        <div className="chain-trace-state">
          <FeedbackState kind="loading" title={t('正在加载链路…')} />
        </div>
      )}
      {trace.state.phase === 'error' && (
        <div className="chain-trace-state">
          <FeedbackState kind="error" title={t('链路加载失败')} onRetry={trace.retry} />
        </div>
      )}
      {incomplete && (
        <div className="chain-trace-state is-warning" role="status">
          <p>{t('链路超过完整视图上限，请使用筛选或搜索缩小范围')}</p>
        </div>
      )}
      {trace.state.phase === 'ready' && !incomplete && nodes.length === 0 && (
        <div className="chain-trace-state"><Activity size={24} /><p>{t('没有匹配的链路节点')}</p></div>
      )}
      {trace.state.phase === 'ready' && !incomplete && nodes.length > 0 && timeline && (
        <>
          {view === 'timeline' && (
            <TraceTimeline
              layout={timeline}
              selected={selected}
              backgroundInert={detailsOverlay && Boolean(selected)}
              onSelect={(nodeId, trigger) => selectEntry(nodeId, trigger, true)}
            />
          )}
          <div
            ref={contentRef}
            id={view === 'timeline' ? 'chain-trace-timeline-panel' : 'chain-trace-tree-panel'}
            role="tabpanel"
            aria-label={view === 'timeline' ? t('时间线') : t('树形')}
            className={`chain-trace-content-grid${selected ? ' has-details' : ''}${detailsOverlay ? ' uses-overlay' : ''}`}
          >
            {view === 'timeline' ? (
              <TraceLedger
                groups={turnRows}
                now={now}
                selectedId={selectedId}
                backgroundInert={detailsOverlay && Boolean(selected)}
                onSelect={selectEntry}
              />
            ) : (
              <TraceTurnTree
                turns={page!.turns}
                nodes={graphNodes}
                orderedNodeIds={page!.orderedNodeIds}
                rootNodeIds={page!.rootNodeIds}
                visibleNodeIds={visibleNodeIds}
                selectedId={selectedId}
                forceExpandAll={categories.size !== TRACE_PUBLIC_CATEGORIES.length || Boolean(searchQuery)}
                forceExpandKey={`${categoryKey}:${showTechnical}:${searchQuery}`}
                backgroundInert={detailsOverlay && Boolean(selected)}
                onSelect={selectEntry}
              />
            )}
            {selected && (
              <TraceDetails
                key={selected.id}
                entry={selected}
                responseEntries={responseEntries}
                responseStatus={responseStatus === 'idle' ? 'loading' : responseStatus}
                turnOrdinal={selectedPosition?.turnOrdinal}
                stepOrdinal={selectedPosition?.stepOrdinal}
                focusClose={detailsOverlay && Boolean(detailTrigger.current)}
                onRetryResponse={modelResponse.retry}
                onClose={closeDetails}
              />
            )}
          </div>
        </>
      )}
    </section>
  )
}
