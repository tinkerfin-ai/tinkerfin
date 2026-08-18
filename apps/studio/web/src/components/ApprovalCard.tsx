import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Pencil,
} from 'lucide-react'

import type { ApprovalDecision, ApprovalState, Conversation, JsonValue } from '../types'
import { normalizeEscapedText } from '../lib/text'
import { MarkdownContent } from './MarkdownContent'
import { TypographyReveal } from './TypographyReveal'

const descriptionParts = (description: string) => {
  const normalized = normalizeEscapedText(description).trim()
  const [title, ...rest] = normalized.split(/\n\s*\n/).filter(Boolean)
  return {
    title: title || '请确认本次操作',
    detail: rest.join('\n\n'),
  }
}

const valueLabel = (value: JsonValue) => {
  if (typeof value === 'string') return value
  if (value == null) return 'null'
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

export function ApprovalCard({
  conversation,
  onChange,
  onSubmit,
}: {
  conversation: Conversation
  onChange: (updater: (approval: ApprovalState) => ApprovalState) => void
  onSubmit: (interruptIds: readonly string[]) => void
}) {
  const approval = conversation.approval
  if (!approval) return null
  const active = approval.items[approval.activeIndex]
  const decided = approval.items.filter((item) => item.decision).length
  const canApprove = active.allowedDecisions.includes('approve')
  const canEdit = active.allowedDecisions.includes('edit')
  const canReject = active.allowedDecisions.includes('reject')
  const args = active.editedArgs ?? active.originalArgs
  const argEntries = Object.entries(args)
  const description = descriptionParts(active.description)
  const interruptIds = approval.items.map((item) => item.interruptId)

  const updateApproval = (updater: (current: ApprovalState) => ApprovalState) => {
    onChange((current) => {
      const isSameGroup = current.items.length === interruptIds.length
        && current.items.every((item, index) => item.interruptId === interruptIds[index])
      return isSameGroup ? updater(current) : current
    })
  }

  const activeInterruptId = active.interruptId
  const currentActiveIndex = (current: ApprovalState) => {
    const matchingIndex = current.items.findIndex(
      (item) => item.interruptId === activeInterruptId,
    )
    return matchingIndex >= 0 ? matchingIndex : current.activeIndex
  }

  const setMode = (mode: NonNullable<typeof approval.mode>) => updateApproval((current) => ({
    ...current,
    mode,
  }))

  const decide = (decision: ApprovalDecision) => {
    updateApproval((current) => {
      const activeIndex = currentActiveIndex(current)
      const items = current.items.map((item, index) =>
        index === activeIndex ? { ...item, decision } : item,
      )
      const nextUndecided = items.findIndex((item) => !item.decision)
      return {
        ...current,
        items,
        activeIndex: nextUndecided < 0 ? activeIndex : nextUndecided,
        mode: 'options',
      }
    })
  }

  const decideAll = (decision: ApprovalDecision) => {
    updateApproval((current) => ({
      ...current,
      items: current.items.map((item) => ({ ...item, decision })),
      error: undefined,
    }))
  }

  const saveEditedApproval = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const editedParams = String(data.get('params') ?? '')
    try {
      const editedArgs = JSON.parse(editedParams)
      if (!editedArgs || typeof editedArgs !== 'object' || Array.isArray(editedArgs)) {
        throw new Error('editedArgs 必须是 JSON 对象')
      }
      updateApproval((current) => {
        const activeIndex = currentActiveIndex(current)
        const items = current.items.map((item, index) => index === activeIndex
          ? { ...item, editedParams, editedArgs, decision: 'approved' as const }
          : item)
        const nextUndecided = items.findIndex((item) => !item.decision)
        return {
          ...current,
          items,
          activeIndex: nextUndecided < 0 ? activeIndex : nextUndecided,
          mode: 'options',
          error: undefined,
        }
      })
    } catch {
      updateApproval((current) => ({
        ...current,
        error: '编辑后的参数必须是合法 JSON 对象。',
      }))
    }
  }

  const submitApproval = () => {
    if (decided !== approval.items.length) return
    updateApproval((current) => ({
      ...current,
      submitted: true,
      error: undefined,
    }))
    onSubmit(interruptIds)
  }

  const confirmRejection = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    updateApproval((current) => {
      const activeIndex = currentActiveIndex(current)
      const items = current.items.map((item, index) => index === activeIndex
        ? { ...item, rejectionReason: String(data.get('reason') ?? ''), decision: 'rejected' as const }
        : item)
      const nextUndecided = items.findIndex((item) => !item.decision)
      return {
        ...current,
        items,
        activeIndex: nextUndecided < 0 ? activeIndex : nextUndecided,
        mode: 'options',
        error: undefined,
      }
    })
  }

  return (
    <div className="approval-card">
      <div className="approval-head">
        <div>
          <span className="eyebrow"><CircleAlert size={14} />等待你的确认…</span>
          <TypographyReveal as="h3" variant="state" revealKey={active.interruptId}>
            {description.title}
          </TypographyReveal>
        </div>
        <div className="approval-pager">
          <button aria-label="上一项审批" disabled={approval.activeIndex === 0} onClick={() => updateApproval((current) => ({ ...current, activeIndex: Math.max(0, current.activeIndex - 1) }))}><ArrowLeft size={15} /></button>
          <span>{approval.activeIndex + 1} / {approval.items.length}</span>
          <button aria-label="下一项审批" disabled={approval.activeIndex === approval.items.length - 1} onClick={() => updateApproval((current) => ({ ...current, activeIndex: Math.min(current.items.length - 1, current.activeIndex + 1) }))}><ArrowRight size={15} /></button>
        </div>
      </div>
      <div className="approval-operation">
        {description.detail && (
          <div className="approval-rich-field approval-detail-field">
            <MarkdownContent content={description.detail} />
          </div>
        )}
        <div className="approval-args-shell">
          {argEntries.length ? (
            <table className="approval-args-table">
              <thead>
                <tr>
                  <th scope="col">输入参数</th>
                  <th scope="col">值</th>
                </tr>
              </thead>
              <tbody>
                {argEntries.map(([key, value]) => (
                  <tr key={key}>
                    <th scope="row">{key}</th>
                    <td>
                      <pre className="approval-value-field"><code>{valueLabel(value)}</code></pre>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="approval-empty-value">无输入参数</div>
          )}
        </div>
      </div>
      {approval.error && <p className="approval-question danger-text">{approval.error}</p>}
      {active.decision ? (
        <div className="decision-made"><CheckCircle2 size={16} />当前项已决定：{active.decision === 'approved' ? '允许' : '拒绝'}</div>
      ) : approval.mode === 'edit' ? (
        <form className="approval-form" onSubmit={saveEditedApproval}>
          <label htmlFor={`approval-params-${active.id}`}>编辑参数（JSON 对象）</label>
          <textarea id={`approval-params-${active.id}`} name="params" defaultValue={active.editedParams ?? active.params} rows={4} />
          <div><button type="button" className="secondary" onClick={() => setMode('options')}>取消</button><button className="primary">保存并允许</button></div>
        </form>
      ) : approval.mode === 'reject' ? (
        <form className="approval-form" onSubmit={confirmRejection}>
          <label htmlFor={`approval-reason-${active.id}`}>拒绝原因（可选）</label>
          <textarea id={`approval-reason-${active.id}`} name="reason" defaultValue={active.rejectionReason} rows={3} placeholder="说明拒绝此操作的原因…" />
          <div><button type="button" className="secondary" onClick={() => setMode('options')}>取消</button><button className="danger">确认拒绝</button></div>
        </form>
      ) : (
        <>
          <p className="approval-question">允许此操作？</p>
          <div className="approval-actions">
            {canReject && <button className="secondary danger-text" onClick={() => setMode('reject')}>拒绝</button>}
            {canEdit && <button className="secondary" onClick={() => setMode('edit')}><Pencil size={13} />编辑</button>}
            {canApprove && <button className="primary" onClick={() => decide('approved')}>允许</button>}
          </div>
        </>
      )}
      <div className="approval-batch">
        <span>已处理 {decided} / {approval.items.length}</span>
        <div>{decided === approval.items.length
          ? <button className="submit-approval" onClick={submitApproval}>批量提交</button>
          : <>
              {approval.items.every((item) => item.allowedDecisions.includes('reject')) && <button onClick={() => decideAll('rejected')}>全部拒绝</button>}
              {approval.items.every((item) => item.allowedDecisions.includes('approve')) && <button onClick={() => decideAll('approved')}>全部允许</button>}
            </>}</div>
      </div>
    </div>
  )
}
