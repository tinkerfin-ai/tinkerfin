import {
  BookOpen,
  Bot,
  BrainCircuit,
  ChevronDown,
  Database,
  GitFork,
  Link2,
  MessageCircle,
  MessageSquareText,
  Puzzle,
  Route,
  Search,
  ShieldCheck,
  UserRound,
  Waypoints,
  Wrench,
} from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

import type {
  TraceGraphNode,
  TraceGraphNodeKind,
  TraceGraphTurn,
} from '../../../api/conversation/traceGraph'
import { useI18n } from '../../../i18n'
import type { JsonValue } from '../../../types'
import {
  durationLabel,
  elapsedMilliseconds,
  traceKindLabel,
  traceNodeName,
} from './tracePresentation'

interface TraceTreeNode {
  node: TraceGraphNode
  children: TraceTreeNode[]
}

interface TraceNodeRowStyle extends CSSProperties {
  '--chain-trace-row-inset': string
}

interface TraceTurnBranch {
  turn: TraceGraphTurn
  roots: TraceTreeNode[]
}

const traceNodeRowStyle = (depth: number): TraceNodeRowStyle => ({
  '--chain-trace-row-inset': `calc(var(--chain-trace-tree-inline-gutter) + var(--chain-trace-root-indent)${' + var(--chain-trace-level-indent)'.repeat(depth)})`,
})

const nodeIcon = (kind: TraceGraphNodeKind) => {
  switch (kind) {
    case 'human_message': return <UserRound size={14} aria-hidden="true" />
    case 'assistant_message': return <MessageCircle size={14} aria-hidden="true" />
    case 'system_message': return <MessageSquareText size={14} aria-hidden="true" />
    case 'agent': return <Route size={14} aria-hidden="true" />
    case 'model': return <BrainCircuit size={14} aria-hidden="true" />
    case 'tool': return <Wrench size={14} aria-hidden="true" />
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

const readableContent = (content: JsonValue | null | undefined) => {
  if (typeof content === 'string') return content
  if (content == null) return ''
  return JSON.stringify(content)
}

const buildTraceTurnForest = (
  turns: TraceGraphTurn[],
  nodes: TraceGraphNode[],
  orderedNodeIds: string[],
  rootNodeIds: string[],
): TraceTurnBranch[] => {
  const turnById = new Map<string, TraceGraphTurn>()
  turns.forEach((turn) => {
    if (turnById.has(turn.id)) throw new Error('Duplicate Trace Turn ID')
    turnById.set(turn.id, turn)
  })
  const sourceById = new Map(nodes.map((node) => [node.id, node]))
  if (sourceById.size !== nodes.length || orderedNodeIds.length !== nodes.length) {
    throw new Error('Trace Graph node order is incomplete')
  }
  const orderedNodes = orderedNodeIds.map((nodeId) => {
    const node = sourceById.get(nodeId)
    if (!node) throw new Error('Trace Graph order references an unknown node')
    if (!turnById.has(node.turnId)) throw new Error('Trace Graph node has no Turn')
    return node
  })
  const treeById = new Map<string, TraceTreeNode>(orderedNodes.map((node) => [
    node.id,
    { node, children: [] } satisfies TraceTreeNode,
  ]))
  const roots = new Set(rootNodeIds)
  if (roots.size !== rootNodeIds.length) throw new Error('Duplicate Trace Graph root')
  orderedNodes.forEach((node) => {
    const current = treeById.get(node.id)
    if (!current) throw new Error('Trace Graph node is unavailable')
    if (node.parentId) {
      const parent = treeById.get(node.parentId)
      if (!parent || roots.has(node.id)) throw new Error('Trace Graph parent is invalid')
      parent.children.push(current)
      return
    }
    if (!roots.has(node.id)) throw new Error('Trace Graph root list is incomplete')
  })
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (treeNode: TraceTreeNode) => {
    if (visited.has(treeNode.node.id)) return
    if (visiting.has(treeNode.node.id)) throw new Error('Trace Graph parent cycle')
    visiting.add(treeNode.node.id)
    treeNode.children.forEach(visit)
    visiting.delete(treeNode.node.id)
    visited.add(treeNode.node.id)
  }
  const rootNodes = orderedNodes
    .filter((node) => roots.has(node.id))
    .map((node) => treeById.get(node.id) as TraceTreeNode)
  rootNodes.forEach(visit)
  if (visited.size !== nodes.length) throw new Error('Trace Graph contains an orphan')
  const rootsByTurn = new Map<string, TraceTreeNode[]>()
  rootNodes.forEach((root) => {
    rootsByTurn.set(root.node.turnId, [
      ...(rootsByTurn.get(root.node.turnId) ?? []),
      root,
    ])
  })
  return turns.flatMap((turn) => {
    const turnRoots = rootsByTurn.get(turn.id) ?? []
    return turnRoots.length ? [{ turn, roots: turnRoots }] : []
  })
}

function TraceNodeItem({
  treeNode,
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
  treeNode: TraceTreeNode
  path: string
  treeId: string
  selectedId?: string
  selectedPathIds: ReadonlySet<string>
  collapsedIds: ReadonlySet<string>
  depth: number
  pathPosition: 'before' | 'incoming' | 'none'
  last: boolean
  onToggle: (nodeId: string) => void
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const node = treeNode.node
  const hasChildren = treeNode.children.length > 0
  const collapsed = hasChildren && collapsedIds.has(node.id)
  const childrenId = `${treeId}-node-${path}`
  const failureSummary = node.failure?.message ?? node.failure?.errorType
  const contentSummary = node.kind.endsWith('_message')
    ? readableContent(node.content)
    : ''
  const selected = selectedId === node.id
  const selectedAncestor = !selected && selectedPathIds.has(node.id)
  const displayName = traceNodeName(node, t)
  return (
    <li className={`chain-trace-node${last ? ' is-last' : ''}${pathPosition === 'before' ? ' is-path-before' : ''}${pathPosition === 'incoming' ? ' is-path-incoming' : ''}${selectedPathIds.has(node.id) ? ' is-on-selected-path' : ''}`}>
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
            aria-label={collapsed ? t('展开 {name}', { name: displayName }) : t('收起 {name}', { name: displayName })}
            aria-expanded={!collapsed}
            aria-controls={childrenId}
            onClick={() => onToggle(node.id)}
          >
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        ) : <span className="chain-trace-node-toggle-spacer" />}
        <button
          type="button"
          className="chain-trace-node-select"
          data-trace-node-id={node.id}
          aria-label={`${traceKindLabel(node.kind, t)}，${displayName}`}
          aria-expanded={selected}
          aria-controls={selected ? 'chain-trace-details' : undefined}
          onClick={(event) => onSelect(node.id, event.currentTarget)}
        >
          <span className={`chain-trace-entry-icon is-${node.kind}`}>
            {nodeIcon(node.kind)}
          </span>
          <span className="chain-trace-node-copy">
            <span className="chain-trace-node-title">
              <strong>{displayName}</strong>
              <small>{traceKindLabel(node.kind, t)}</small>
            </span>
            {contentSummary && (
              <span className="chain-trace-node-content">{contentSummary}</span>
            )}
            {failureSummary && (
              <span className="chain-trace-error-summary">{failureSummary}</span>
            )}
          </span>
          <span className="chain-trace-node-meta">
            {node.failure && <span className="chain-trace-error-badge">{t('错误')}</span>}
            {!node.failure && node.status === 'waiting' && (
              <span className="chain-trace-waiting-badge">{t('等待中')}</span>
            )}
            <span className="chain-trace-duration">
              {durationLabel(elapsedMilliseconds(node), t)}
            </span>
          </span>
        </button>
      </div>
      {hasChildren && !collapsed && (
        <TraceNodeList
          id={childrenId}
          nodes={treeNode.children}
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
  onToggle: (nodeId: string) => void
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const selectedChildIndex = nodes.findIndex((node) => (
    selectedPathIds.has(node.node.id)
  ))
  return (
    <ol
      id={id}
      className={`chain-trace-node-list${root ? ' is-root' : ''}${selectedChildIndex >= 0 ? ' has-selected-path' : ''}`}
    >
      {nodes.map((treeNode, index) => (
        <TraceNodeItem
          key={treeNode.node.id}
          treeNode={treeNode}
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
  nodes,
  orderedNodeIds,
  rootNodeIds,
  selectedId,
  forceExpandAll = false,
  forceExpandKey,
  backgroundInert = false,
  onSelect,
}: {
  turns: TraceGraphTurn[]
  nodes: TraceGraphNode[]
  orderedNodeIds: string[]
  rootNodeIds: string[]
  selectedId?: string
  forceExpandAll?: boolean
  forceExpandKey?: string
  backgroundInert?: boolean
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const treeId = useId()
  const forest = useMemo(
    () => buildTraceTurnForest(turns, nodes, orderedNodeIds, rootNodeIds),
    [nodes, orderedNodeIds, rootNodeIds, turns],
  )
  const nodesById = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  )
  const selectedPathIds = useMemo(() => {
    const path = new Set<string>()
    let nodeId: string | null | undefined = selectedId
    while (nodeId && !path.has(nodeId)) {
      path.add(nodeId)
      nodeId = nodesById.get(nodeId)?.parentId
    }
    return path
  }, [nodesById, selectedId])
  const errorTurnIds = useMemo(
    () => new Set(nodes.filter((node) => node.failure).map((node) => node.turnId)),
    [nodes],
  )
  const initialTurnIds = forest.map(({ turn }) => turn.id)
  const knownTurnIds = useRef(new Set(initialTurnIds))
  const [collapsedNodeIds, setCollapsedNodeIds] = useState<Set<string>>(() => (
    new Set(forest.slice(0, -1).flatMap(({ turn, roots }) => (
      errorTurnIds.has(turn.id) ? [] : roots.map((root) => root.node.id)
    )))
  ))
  const [filteredCollapsedNodeIds, setFilteredCollapsedNodeIds] = useState<Set<string>>(new Set())
  const turnKey = forest.map(({ turn }) => turn.id).join('\u0000')
  const errorTurnKey = [...errorTurnIds].sort().join('\u0000')

  useEffect(() => {
    const currentTurnIds = new Set(turnKey ? turnKey.split('\u0000') : [])
    const currentErrorTurnIds = new Set(errorTurnKey ? errorTurnKey.split('\u0000') : [])
    const latestTurnId = [...currentTurnIds].at(-1)
    const currentNodeIds = new Set(nodesById.keys())
    setCollapsedNodeIds((current) => {
      const next = new Set([...current].filter((nodeId) => currentNodeIds.has(nodeId)))
      forest.forEach(({ turn, roots }) => {
        if (knownTurnIds.current.has(turn.id)) return
        roots.forEach((root) => {
          if (turn.id !== latestTurnId && !currentErrorTurnIds.has(turn.id)) {
            next.add(root.node.id)
          } else {
            next.delete(root.node.id)
          }
        })
      })
      forest.forEach(({ turn, roots }) => {
        if (currentErrorTurnIds.has(turn.id)) {
          roots.forEach((root) => next.delete(root.node.id))
        }
      })
      return next
    })
    knownTurnIds.current = currentTurnIds
  }, [errorTurnKey, forest, nodesById, turnKey])

  useEffect(() => {
    if (forceExpandAll) setFilteredCollapsedNodeIds(new Set())
  }, [forceExpandAll, forceExpandKey])

  const activeCollapsedNodeIds = forceExpandAll
    ? filteredCollapsedNodeIds
    : collapsedNodeIds
  const toggleNodeState = forceExpandAll
    ? setFilteredCollapsedNodeIds
    : setCollapsedNodeIds
  const toggleNode = (nodeId: string) => toggleNodeState((current) => {
    const next = new Set(current)
    if (next.has(nodeId)) next.delete(nodeId)
    else next.add(nodeId)
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
        const selectedTurnPath = roots.some((root) => (
          selectedPathIds.has(root.node.id)
        ))
        const branchId = `${treeId}-turn-${turnIndex}`
        return (
          <section
            className={`chain-trace-turn${selectedTurnPath ? ' is-selected-path' : ''}`}
            aria-label={`${turn.ordinal}`}
            key={turn.id}
          >
            <TraceNodeList
              id={branchId}
              nodes={roots}
              path={`${turnIndex}`}
              treeId={treeId}
              selectedId={selectedId}
              selectedPathIds={selectedPathIds}
              collapsedIds={activeCollapsedNodeIds}
              root
              onToggle={toggleNode}
              onSelect={onSelect}
            />
          </section>
        )
      })}
    </div>
  )
}
