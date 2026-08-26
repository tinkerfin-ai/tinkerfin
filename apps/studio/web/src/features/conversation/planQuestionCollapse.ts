const APPROVAL_COLLAPSE_KEY_PREFIX = 'tinkerfin:approval-collapse:'
const PLAN_QUESTION_COLLAPSE_KEY_PREFIX = 'tinkerfin:plan-question-collapse:'
const PLAN_REVIEW_COLLAPSE_KEY_PREFIX = 'tinkerfin:plan-review-collapse:'

type InteractionCardKind = 'approval' | 'question' | 'review'

const prefixFor = (kind: InteractionCardKind) => {
  if (kind === 'approval') return APPROVAL_COLLAPSE_KEY_PREFIX
  return kind === 'question'
    ? PLAN_QUESTION_COLLAPSE_KEY_PREFIX
    : PLAN_REVIEW_COLLAPSE_KEY_PREFIX
}

const interactionCardCollapseKey = (kind: InteractionCardKind, threadId: string) => (
  `${prefixFor(kind)}${threadId}`
)

export const approvalCollapseKey = (threadId: string) => (
  interactionCardCollapseKey('approval', threadId)
)

export const planQuestionCollapseKey = (threadId: string) => (
  interactionCardCollapseKey('question', threadId)
)

export const planReviewCollapseKey = (threadId: string) => (
  interactionCardCollapseKey('review', threadId)
)

const readInteractionCardCollapsed = (
  kind: InteractionCardKind,
  threadId: string,
): boolean => {
  if (!threadId) return false
  try {
    return window.sessionStorage.getItem(interactionCardCollapseKey(kind, threadId)) === 'collapsed'
  } catch {
    return false
  }
}

const writeInteractionCardCollapsed = (
  kind: InteractionCardKind,
  threadId: string,
  collapsed: boolean,
): void => {
  if (!threadId) return
  try {
    window.sessionStorage.setItem(
      interactionCardCollapseKey(kind, threadId),
      collapsed ? 'collapsed' : 'expanded',
    )
  } catch {
    // 浏览器禁用会话存储时保留当前页面内的展开收起行为
  }
}

export function readApprovalCollapsed(threadId: string): boolean {
  return readInteractionCardCollapsed('approval', threadId)
}

export function writeApprovalCollapsed(threadId: string, collapsed: boolean): void {
  writeInteractionCardCollapsed('approval', threadId, collapsed)
}

export function readPlanQuestionCollapsed(threadId: string): boolean {
  return readInteractionCardCollapsed('question', threadId)
}

export function writePlanQuestionCollapsed(threadId: string, collapsed: boolean): void {
  writeInteractionCardCollapsed('question', threadId, collapsed)
}

export function readPlanReviewCollapsed(threadId: string): boolean {
  return readInteractionCardCollapsed('review', threadId)
}

export function writePlanReviewCollapsed(threadId: string, collapsed: boolean): void {
  writeInteractionCardCollapsed('review', threadId, collapsed)
}

export function clearInteractionCardCollapsed(threadId: string): void {
  if (!threadId) return
  try {
    window.sessionStorage.removeItem(approvalCollapseKey(threadId))
    window.sessionStorage.removeItem(planQuestionCollapseKey(threadId))
    window.sessionStorage.removeItem(planReviewCollapseKey(threadId))
  } catch {
    // 清理失败不应阻止会话删除
  }
}
