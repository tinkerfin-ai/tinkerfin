import {
  BookOpen,
  Bot,
  BrainCircuit,
  ChevronDown,
  Database,
  GitFork,
  Link2,
  Puzzle,
  Route,
  Search,
  ShieldCheck,
  Sparkles,
  Waypoints,
  Workflow,
  Wrench,
} from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

import type {
  TraceEntry,
  TraceEntryKind,
  TraceTurn,
} from '../../../api/conversation/traceEntries'
import { useI18n } from '../../../i18n'
import type { JsonValue } from '../../../types'
import {
  durationLabel,
  elapsedMilliseconds,
  traceKindLabel,
} from './tracePresentation'

interface TraceTreeNode {
  entry: TraceEntry
  children: TraceTreeNode[]
}

interface TraceNodeRowStyle extends CSSProperties {
  '--chain-trace-row-inset': string
}

const traceNodeRowStyle = (depth: number): TraceNodeRowStyle => ({
  '--chain-trace-row-inset': `calc(var(--chain-trace-tree-inline-gutter) + var(--chain-trace-root-indent)${' + var(--chain-trace-level-indent)'.repeat(depth)})`,
})

interface TraceTurnBranch {
  turn: TraceTurn
  roots: TraceTreeNode[]
}

const entryIcon = (kind: TraceEntryKind) => {
  switch (kind) {
    case 'agent': return <Route size={14} aria-hidden="true" />
    case 'model': return <BrainCircuit size={14} aria-hidden="true" />
    case 'provider': return <Sparkles size={14} aria-hidden="true" />
    case 'tool':
    case 'tool_proposal': return <Wrench size={14} aria-hidden="true" />
    case 'tools': return <Workflow size={14} aria-hidden="true" />
    case 'subagent': return <GitFork size={14} aria-hidden="true" />
    case 'skill': return <BookOpen size={14} aria-hidden="true" />
    case 'memory': return <Database size={14} aria-hidden="true" />
    case 'retrieval': return <Search size={14} aria-hidden="true" />
    case 'guardrail': return <ShieldCheck size={14} aria-hidden="true" />
    case 'middleware': return <Link2 size={14} aria-hidden="true" />
    case 'run': return <Bot size={14} aria-hidden="true" />
    case 'task': return <Waypoints size={14} aria-hidden="true" />
    default: return <Puzzle size={14} aria-hidden="true" />
  }
}

const compareEntries = (left: TraceEntry, right: TraceEntry) => (
  left.startedSeq - right.startedSeq || left.id.localeCompare(right.id)
)

const buildTraceTurnForest = (
  turns: TraceTurn[],
  entries: TraceEntry[],
  hiddenKinds: ReadonlySet<TraceEntryKind>,
): TraceTurnBranch[] => {
  const turnById = new Map<string, TraceTurn>()
  turns.forEach((turn) => {
    if (turnById.has(turn.id)) throw new Error('Duplicate Trace Turn ID')
    turnById.set(turn.id, turn)
  })
  const entriesByTurn = new Map<string, TraceEntry[]>()
  const entryIds = new Set<string>()
  entries.forEach((entry) => {
    if (entryIds.has(entry.id)) throw new Error('Duplicate Trace entry ID')
    if (!turnById.has(entry.turnId)) throw new Error('Trace entry has no Turn')
    entryIds.add(entry.id)
    entriesByTurn.set(entry.turnId, [
      ...(entriesByTurn.get(entry.turnId) ?? []),
      entry,
    ])
  })

  return [...turns]
    .sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id))
    .map((turn) => {
      const turnEntries = [...(entriesByTurn.get(turn.id) ?? [])].sort(compareEntries)
      const nodes = new Map<string, TraceTreeNode>(turnEntries.map((entry) => [
        entry.id,
        { entry, children: [] },
      ]))
      const visiting = new Set<string>()
      const visited = new Set<string>()
      const visit = (entry: TraceEntry) => {
        if (visited.has(entry.id)) return
        if (visiting.has(entry.id)) throw new Error('Trace entry parent cycle')
        visiting.add(entry.id)
        const parent = entry.parentId ? nodes.get(entry.parentId)?.entry : undefined
        if (parent) visit(parent)
        visiting.delete(entry.id)
        visited.add(entry.id)
      }
      turnEntries.forEach(visit)

      const roots: TraceTreeNode[] = []
      turnEntries.forEach((entry) => {
        const node = nodes.get(entry.id)
        if (!node) throw new Error('Trace entry node is unavailable')
        const parent = entry.parentId ? nodes.get(entry.parentId) : undefined
        if (parent) parent.children.push(node)
        else roots.push(node)
      })
      nodes.forEach((node) => node.children.sort((left, right) => (
        compareEntries(left.entry, right.entry)
      )))
      roots.sort((left, right) => compareEntries(left.entry, right.entry))
      const visibleNodes = (values: TraceTreeNode[]): TraceTreeNode[] => values.flatMap(
        (node) => {
          const children = visibleNodes(node.children)
          return hiddenKinds.has(node.entry.kind)
            ? children
            : [{ entry: node.entry, children }]
        },
      )
      return { turn, roots: visibleNodes(roots) }
    })
}

const readableContent = (content: JsonValue | null | undefined) => {
  if (typeof content === 'string') return content
  if (content == null) return ''
  return JSON.stringify(content)
}

function TraceNodeItem({
  node,
  path,
  treeId,
  selectedId,
  selectedPathIds,
  collapsedIds,
  depth,
  pathPosition,
  last,
  onToggle,
  onSelect,
}: {
  node: TraceTreeNode
  path: string
  treeId: string
  selectedId?: string
  selectedPathIds: ReadonlySet<string>
  collapsedIds: ReadonlySet<string>
  depth: number
  pathPosition: 'before' | 'incoming' | 'none'
  last: boolean
  onToggle: (entryId: string) => void
  onSelect: (entryId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const hasChildren = node.children.length > 0
  const collapsed = hasChildren && collapsedIds.has(node.entry.id)
  const childrenId = `${treeId}-node-${path}`
  const failureSummary = node.entry.failure?.message ?? node.entry.failure?.errorType
  const configured = node.entry.status === 'configured'
  const selected = selectedId === node.entry.id
  const selectedAncestor = !selected && selectedPathIds.has(node.entry.id)
  return (
    <li className={`chain-trace-node${last ? ' is-last' : ''}${pathPosition === 'before' ? ' is-path-before' : ''}${pathPosition === 'incoming' ? ' is-path-incoming' : ''}${selectedPathIds.has(node.entry.id) ? ' is-on-selected-path' : ''}`}>
      <span className="chain-trace-connector-elbow" aria-hidden="true" />
      <span className="chain-trace-connector-path-before" aria-hidden="true" />
      <span className="chain-trace-connector-tail" aria-hidden="true" />
      <div
        className={`chain-trace-node-row${selected ? ' is-selected' : ''}${selectedAncestor ? ' is-selected-ancestor' : ''}`}
        style={traceNodeRowStyle(depth)}
      >
        {hasChildren ? (
          <button
            type="button"
            className="chain-trace-node-toggle"
            aria-label={collapsed ? t('展开 {name}', { name: node.entry.name }) : t('收起 {name}', { name: node.entry.name })}
            aria-expanded={!collapsed}
            aria-controls={childrenId}
            onClick={() => onToggle(node.entry.id)}
          >
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        ) : <span className="chain-trace-node-toggle-spacer" />}
        <button
          type="button"
          className="chain-trace-node-select"
          data-trace-entry-id={node.entry.id}
          aria-label={`${traceKindLabel(node.entry.kind, t)}，${node.entry.name}`}
          aria-expanded={selected}
          aria-controls={selected ? 'chain-trace-details' : undefined}
          onClick={(event) => onSelect(node.entry.id, event.currentTarget)}
        >
          <span className={`chain-trace-entry-icon is-${node.entry.kind}`}>
            {entryIcon(node.entry.kind)}
          </span>
          <span className="chain-trace-node-copy">
            <span className="chain-trace-node-title">
              <strong>{node.entry.name}</strong>
              <small>{traceKindLabel(node.entry.kind, t)}</small>
            </span>
            {failureSummary && (
              <span className="chain-trace-error-summary">
                {failureSummary}
              </span>
            )}
          </span>
          <span className="chain-trace-node-meta">
            {node.entry.failure && <span className="chain-trace-error-badge">{t('错误')}</span>}
            {!node.entry.failure && node.entry.status === 'waiting' && (
              <span className="chain-trace-waiting-badge">{t('等待中')}</span>
            )}
            <span className="chain-trace-duration">
              {configured ? t('仅配置') : durationLabel(elapsedMilliseconds(node.entry), t)}
            </span>
          </span>
        </button>
      </div>
      {hasChildren && !collapsed && (
        <TraceNodeList
          id={childrenId}
          nodes={node.children}
          path={path}
          treeId={treeId}
          selectedId={selectedId}
          selectedPathIds={selectedPathIds}
          collapsedIds={collapsedIds}
          depth={depth + 1}
          onToggle={onToggle}
          onSelect={onSelect}
        />
      )}
    </li>
  )
}

function TraceNodeList({
  id,
  nodes,
  path,
  treeId,
  selectedId,
  selectedPathIds,
  collapsedIds,
  depth = 0,
  root = false,
  onToggle,
  onSelect,
}: {
  id: string
  nodes: TraceTreeNode[]
  path: string
  treeId: string
  selectedId?: string
  selectedPathIds: ReadonlySet<string>
  collapsedIds: ReadonlySet<string>
  depth?: number
  root?: boolean
  onToggle: (entryId: string) => void
  onSelect: (entryId: string, trigger: HTMLButtonElement) => void
}) {
  const selectedChildIndex = nodes.findIndex((node) => selectedPathIds.has(node.entry.id))
  return (
    <ol
      id={id}
      className={`chain-trace-node-list${root ? ' is-root' : ''}${selectedChildIndex >= 0 ? ' has-selected-path' : ''}`}
    >
      {nodes.map((node, index) => (
        <TraceNodeItem
          key={node.entry.id}
          node={node}
          path={`${path}-${index}`}
          treeId={treeId}
          selectedId={selectedId}
          selectedPathIds={selectedPathIds}
          collapsedIds={collapsedIds}
          depth={depth}
          pathPosition={selectedChildIndex > index
            ? 'before'
            : selectedChildIndex === index ? 'incoming' : 'none'}
          last={index === nodes.length - 1}
          onToggle={onToggle}
          onSelect={onSelect}
        />
      ))}
    </ol>
  )
}

export function TraceTurnTree({
  turns,
  entries,
  selectedId,
  forceExpandAll = false,
  forceExpandKey,
  hiddenKinds = [],
  backgroundInert = false,
  onSelect,
}: {
  turns: TraceTurn[]
  entries: TraceEntry[]
  selectedId?: string
  forceExpandAll?: boolean
  forceExpandKey?: string
  hiddenKinds?: readonly TraceEntryKind[]
  backgroundInert?: boolean
  onSelect: (entryId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const treeId = useId()
  const forest = useMemo(
    () => buildTraceTurnForest(turns, entries, new Set(hiddenKinds)),
    [entries, hiddenKinds, turns],
  )
  const entriesById = useMemo(
    () => new Map(entries.map((entry) => [entry.id, entry])),
    [entries],
  )
  const selectedPathIds = useMemo(() => {
    const path = new Set<string>()
    let entryId: string | null | undefined = selectedId
    while (entryId && !path.has(entryId)) {
      path.add(entryId)
      entryId = entriesById.get(entryId)?.parentId
    }
    return path
  }, [entriesById, selectedId])
  const errorTurnIds = useMemo(
    () => new Set(entries.filter((entry) => entry.failure).map((entry) => entry.turnId)),
    [entries],
  )
  const initialTurnIds = forest.map(({ turn }) => turn.id)
  const knownTurnIds = useRef(new Set(initialTurnIds))
  const [collapsedTurnIds, setCollapsedTurnIds] = useState<Set<string>>(() => (
    new Set(initialTurnIds.slice(0, -1).filter((turnId) => !errorTurnIds.has(turnId)))
  ))
  const [collapsedEntryIds, setCollapsedEntryIds] = useState<Set<string>>(new Set())
  const [filteredCollapsedTurnIds, setFilteredCollapsedTurnIds] = useState<Set<string>>(new Set())
  const [filteredCollapsedEntryIds, setFilteredCollapsedEntryIds] = useState<Set<string>>(new Set())
  const turnKey = forest.map(({ turn }) => turn.id).join('\u0000')
  const errorTurnKey = [...errorTurnIds].sort().join('\u0000')

  useEffect(() => {
    const currentIds = new Set(turnKey ? turnKey.split('\u0000') : [])
    const currentErrorTurnIds = new Set(errorTurnKey ? errorTurnKey.split('\u0000') : [])
    const latestId = [...currentIds].at(-1)
    setCollapsedTurnIds((current) => {
      const next = new Set([...current].filter((turnId) => currentIds.has(turnId)))
      currentIds.forEach((turnId) => {
        if (!knownTurnIds.current.has(turnId) && turnId !== latestId && !currentErrorTurnIds.has(turnId)) {
          next.add(turnId)
        }
      })
      if (latestId && !knownTurnIds.current.has(latestId)) next.delete(latestId)
      currentErrorTurnIds.forEach((turnId) => next.delete(turnId))
      return next
    })
    knownTurnIds.current = currentIds
  }, [errorTurnKey, turnKey])

  useEffect(() => {
    if (!forceExpandAll) return
    setFilteredCollapsedTurnIds(new Set())
    setFilteredCollapsedEntryIds(new Set())
  }, [forceExpandAll, forceExpandKey])

  const toggleTurnState = forceExpandAll ? setFilteredCollapsedTurnIds : setCollapsedTurnIds
  const toggleEntryState = forceExpandAll ? setFilteredCollapsedEntryIds : setCollapsedEntryIds
  const activeCollapsedTurnIds = forceExpandAll ? filteredCollapsedTurnIds : collapsedTurnIds
  const activeCollapsedEntryIds = forceExpandAll ? filteredCollapsedEntryIds : collapsedEntryIds
  const toggleTurn = (turnId: string) => toggleTurnState((current) => {
    const next = new Set(current)
    if (next.has(turnId)) next.delete(turnId)
    else next.add(turnId)
    return next
  })
  const toggleEntry = (entryId: string) => toggleEntryState((current) => {
    const next = new Set(current)
    if (next.has(entryId)) next.delete(entryId)
    else next.add(entryId)
    return next
  })

  return (
    <div
      className="chain-trace-tree-scroll"
      aria-hidden={backgroundInert || undefined}
      inert={backgroundInert || undefined}
      tabIndex={-1}
    >
      {forest.map(({ turn, roots }, turnIndex) => {
        const collapsed = roots.length > 0 && activeCollapsedTurnIds.has(turn.id)
        const selectedTurnPath = roots.some((node) => selectedPathIds.has(node.entry.id))
        const content = turn.userMessage?.contentOmitted
          ? t('HumanMessage 内容未保留')
          : readableContent(turn.userMessage?.content) || t('HumanMessage 内容不可用')
        const branchId = `${treeId}-turn-${turnIndex}`
        return (
          <section className={`chain-trace-turn${selectedTurnPath ? ' is-selected-path' : ''}`} aria-labelledby={`${branchId}-title`} key={turn.id}>
            <div className="chain-trace-human-row">
              {roots.length > 0 ? (
                <button
                  type="button"
                  className="chain-trace-turn-toggle"
                  aria-label={collapsed ? t('展开第 {count} 轮', { count: turn.ordinal }) : t('收起第 {count} 轮', { count: turn.ordinal })}
                  aria-expanded={!collapsed}
                  aria-controls={branchId}
                  onClick={() => toggleTurn(turn.id)}
                >
                  <ChevronDown size={14} aria-hidden="true" />
                </button>
              ) : <span className="chain-trace-node-toggle-spacer" />}
              <span className="chain-trace-human-icon" aria-hidden="true">{turn.ordinal}</span>
              <span className="chain-trace-human-copy">
                <span className="chain-trace-human-title">
                  <strong id={`${branchId}-title`}>{t('HumanMessage')}</strong>
                  <small>{t('第 {count} 轮', { count: turn.ordinal })}</small>
                </span>
                <span>{content}</span>
              </span>
            </div>
            {!collapsed && (
              <TraceNodeList
                id={branchId}
                nodes={roots}
                path={`${turnIndex}`}
                treeId={treeId}
                selectedId={selectedId}
                selectedPathIds={selectedPathIds}
                collapsedIds={activeCollapsedEntryIds}
                root
                onToggle={toggleEntry}
                onSelect={onSelect}
              />
            )}
          </section>
        )
      })}
    </div>
  )
}
