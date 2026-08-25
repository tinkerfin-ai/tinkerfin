import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Pencil,
} from 'lucide-react'
import { useState } from 'react'

import { Button, IconButton, Surface } from '../../../components/ui'
import type { ApprovalDecision, ApprovalState, Conversation, JsonValue } from '../../../types'
import { MarkdownContent } from './MarkdownContent'
import { useI18n } from '../../../i18n'

const descriptionParts = (description: string, fallback: string) => {
  const normalized = description.trim()
  const [title, ...rest] = normalized.split(/\n\s*\n/).filter(Boolean)
  return {
    title: title || fallback,
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
  const { t } = useI18n()
  const approval = conversation.approval
  const [drafts, setDrafts] = useState<Record<string, {
    params?: string
    rejectionReason?: string
  }>>({})
  if (!approval) return null
  const active = approval.items[approval.activeIndex]
  const decided = approval.items.filter((item) => item.decision).length
  const canApprove = active.allowedDecisions.includes('approve')
  const canEdit = active.allowedDecisions.includes('edit')
  const canReject = active.allowedDecisions.includes('reject')
  const args = active.editedArgs ?? active.originalArgs
  const argEntries = Object.entries(args)
  const description = descriptionParts(active.description, t('请确认本次操作'))
  const interruptIds = approval.items.map((item) => item.interruptId)
  const activeDraft = drafts[active.interruptId]

  // 编辑内容属于具体审批项，分页只切换视图，不能把上一项草稿提交给下一项
  const updateActiveDraft = (patch: { params?: string; rejectionReason?: string }) => {
    setDrafts((current) => ({
      ...current,
      [active.interruptId]: { ...current[active.interruptId], ...patch },
    }))
  }

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
    const editedParams = activeDraft?.params ?? active.editedParams ?? active.params
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
        error: t('编辑后的参数必须是合法 JSON 对象'),
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
    const rejectionReason = activeDraft?.rejectionReason ?? active.rejectionReason ?? ''
    updateApproval((current) => {
      const activeIndex = currentActiveIndex(current)
      const items = current.items.map((item, index) => index === activeIndex
        ? { ...item, rejectionReason, decision: 'rejected' as const }
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
    <Surface as="section" tone="danger" elevation={1} className="approval-card">
      <div className="approval-head">
        <div>
          <span className="eyebrow"><CircleAlert size={14} />{t('等待你的确认…')}</span>
          <h3 key={active.interruptId}>{description.title}</h3>
        </div>
        <div className="approval-pager">
          <IconButton label={t('上一项审批')} icon={<ArrowLeft size={15} />} disabled={approval.activeIndex === 0} onClick={() => updateApproval((current) => ({ ...current, activeIndex: Math.max(0, current.activeIndex - 1) }))} />
          <span>{approval.activeIndex + 1} / {approval.items.length}</span>
          <IconButton label={t('下一项审批')} icon={<ArrowRight size={15} />} disabled={approval.activeIndex === approval.items.length - 1} onClick={() => updateApproval((current) => ({ ...current, activeIndex: Math.min(current.items.length - 1, current.activeIndex + 1) }))} />
        </div>
      </div>
      <div className="approval-operation">
        {description.detail && (
          <div className="approval-rich-field approval-detail-field">
          <MarkdownContent content={description.detail} variant="compact" />
          </div>
        )}
        <div className="approval-args-shell">
          {argEntries.length ? (
            <table className="approval-args-table">
              <thead>
                <tr>
                  <th scope="col">{t('输入参数')}</th>
                  <th scope="col">{t('值')}</th>
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
            <div className="approval-empty-value">{t('无输入参数')}</div>
          )}
        </div>
      </div>
      {approval.error && <p className="approval-question danger-text" role="alert">{approval.error}</p>}
      {active.decision ? (
        <div className="decision-made"><CheckCircle2 size={16} />{t('当前项已决定：{decision}', { decision: active.decision === 'approved' ? t('允许') : t('拒绝') })}</div>
      ) : approval.mode === 'edit' ? (
        <form key={`edit:${active.interruptId}`} className="approval-form" onSubmit={saveEditedApproval}>
          <label htmlFor={`approval-params-${active.id}`}>{t('编辑参数（JSON 对象）')}</label>
          <textarea id={`approval-params-${active.id}`} name="params" value={activeDraft?.params ?? active.editedParams ?? active.params} rows={4} onChange={(event) => updateActiveDraft({ params: event.currentTarget.value })} />
          <div><Button onClick={() => setMode('options')}>{t('取消')}</Button><Button type="submit" variant="primary">{t('保存并允许')}</Button></div>
        </form>
      ) : approval.mode === 'reject' ? (
        <form key={`reject:${active.interruptId}`} className="approval-form" onSubmit={confirmRejection}>
          <label htmlFor={`approval-reason-${active.id}`}>{t('拒绝原因（可选）')}</label>
          <textarea id={`approval-reason-${active.id}`} name="reason" value={activeDraft?.rejectionReason ?? active.rejectionReason ?? ''} rows={3} placeholder={t('说明拒绝此操作的原因…')} onChange={(event) => updateActiveDraft({ rejectionReason: event.currentTarget.value })} />
          <div><Button onClick={() => setMode('options')}>{t('取消')}</Button><Button type="submit" variant="danger">{t('确认拒绝')}</Button></div>
        </form>
      ) : (
        <>
          <p className="approval-question">{t('允许此操作？')}</p>
          <div className="approval-actions">
            {canReject && <Button className="danger-text" onClick={() => setMode('reject')}>{t('拒绝')}</Button>}
            {canEdit && <Button leadingIcon={<Pencil size={14} />} onClick={() => setMode('edit')}>{t('编辑')}</Button>}
            {canApprove && <Button variant="primary" onClick={() => decide('approved')}>{t('允许')}</Button>}
          </div>
        </>
      )}
      <div className="approval-batch">
        <span>{t('已处理 {decided} / {total}', { decided, total: approval.items.length })}</span>
        <div>{decided === approval.items.length
          ? <Button variant="primary" size="sm" className="submit-approval" onClick={submitApproval}>{t('批量提交')}</Button>
          : <>
              {approval.items.every((item) => item.allowedDecisions.includes('reject')) && <Button variant="text" size="sm" onClick={() => decideAll('rejected')}>{t('全部拒绝')}</Button>}
              {approval.items.every((item) => item.allowedDecisions.includes('approve')) && <Button variant="text" size="sm" onClick={() => decideAll('approved')}>{t('全部允许')}</Button>}
            </>}</div>
      </div>
    </Surface>
  )
}
