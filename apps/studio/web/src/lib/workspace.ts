import type { Conversation, WorkspaceState } from '../types'

export const TRANSIENT_THREAD_ID = ''

const sortConversations = (conversations: Conversation[]) =>
  [...conversations].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))

export const buildEmptyConversation = (
  options: { threadId?: string; now: string; model?: string },
): Conversation => ({
  threadId: options.threadId ?? TRANSIENT_THREAD_ID,
  title: '新会话',
  pinned: false,
  updatedAt: options.now,
  model: options.model ?? 'GPT-5.5',
  messages: [],
  todos: [],
  plan: null,
  runStatus: 'idle',
  isHydrated: true,
})

export const createEmptyWorkspace = (): WorkspaceState => ({
  conversations: [],
  currentThreadId: TRANSIENT_THREAD_ID,
})

export function createNewConversation(
  state: WorkspaceState,
): WorkspaceState {
  return {
    ...state,
    currentThreadId: TRANSIENT_THREAD_ID,
  }
}

export function updateConversation(
  state: WorkspaceState,
  threadId: string,
  updater: (conversation: Conversation) => Conversation,
): WorkspaceState {
  return {
    ...state,
    conversations: state.conversations.map((conversation) =>
      conversation.threadId === threadId ? updater(conversation) : conversation,
    ),
  }
}

export function upsertConversation(
  state: WorkspaceState,
  nextConversation: Conversation,
): WorkspaceState {
  const hasConversation = state.conversations.some(
    (conversation) => conversation.threadId === nextConversation.threadId,
  )
  const conversations = hasConversation
    ? state.conversations.map((conversation) =>
        conversation.threadId === nextConversation.threadId
          ? nextConversation
          : conversation)
    : [nextConversation, ...state.conversations]
  return {
    ...state,
    conversations: sortConversations(conversations),
  }
}

export function removeConversation(
  state: WorkspaceState,
  threadId: string,
): WorkspaceState {
  const conversations = state.conversations.filter(
    (conversation) => conversation.threadId !== threadId,
  )
  return {
    conversations,
    currentThreadId:
      state.currentThreadId === threadId
        ? (conversations[0]?.threadId ?? TRANSIENT_THREAD_ID)
        : state.currentThreadId,
  }
}
