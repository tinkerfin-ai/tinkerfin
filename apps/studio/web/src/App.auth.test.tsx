import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import {
  AUTH_SESSION_STORAGE_KEY,
  clearAuthSession,
  saveAuthSession,
} from './auth/session'
import {
  clearActiveRunSession,
  readActiveRunSession,
  writeActiveRunSession,
} from './features/conversation/stream/activeRunSession'
import { emptyTraceGraph } from './test/traceFixtures'

const user = {
  user_id: 7,
  username: 'yunsan',
  display_name: '云杉',
  avatar_url: null,
  roles: [],
  disabled: false,
}

function envelope(data: unknown, status = 200, code = 0, message = 'success') {
  return new Response(JSON.stringify({ code, message, data }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function loginPayload(accessToken = 'fresh-token') {
  return {
    access_token: accessToken,
    token_type: 'Bearer',
    expires_at: '2099-01-01T00:00:00.000Z',
    user,
  }
}

function sessionPayload(expiresAt = '2099-01-01T00:00:00.000Z') {
  return {
    expires_at: expiresAt,
    user,
  }
}

function seedSession(
  token = 'token-123',
  expiresAt = '2099-01-01T00:00:00.000Z',
) {
  saveAuthSession({
    token,
    tokenType: 'Bearer',
    expiresAt,
    user,
  })
}

describe('App authentication boundary', () => {
  afterEach(() => {
    cleanup()
    clearAuthSession()
    clearActiveRunSession()
    window.history.replaceState(null, '', '/')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('shows the login screen without mounting the workspace when no session exists', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState(null, '', '/?thread=private-thread')

    render(<App />)

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps the workspace unmounted while the stored session is being verified', () => {
    seedSession('token-pending')
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))

    render(<App />)

    expect(screen.getByLabelText('正在检查登录状态')).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '欢迎回来' })).not.toBeInTheDocument()
  })

  it('silently clears an invalid session and thread route before showing login', async () => {
    seedSession()
    window.history.replaceState(null, '', '/?thread=private-thread')
    vi.stubGlobal('fetch', vi.fn(async () => envelope(null, 401, 1_001_001_000, '登录已过期')))

    render(<App />)

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('mounts the workspace only after the stored session is verified', async () => {
    seedSession()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: '欢迎回来' })).not.toBeInTheDocument()
  })

  it('keeps the session blocked and retries when /me is temporarily unavailable', async () => {
    seedSession('retry-token')
    let sessionRequests = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) {
        sessionRequests += 1
        return sessionRequests === 1
          ? new Response(null, { status: 503 })
          : envelope(sessionPayload())
      }
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByLabelText('正在重新验证登录状态')).toBeInTheDocument()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).not.toBeNull()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument(), {
      timeout: 2500,
    })
    expect(sessionRequests).toBe(2)
  })

  it('retries a network verification failure immediately when connectivity returns', async () => {
    seedSession('network-retry-token')
    let sessionRequests = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) {
        sessionRequests += 1
        if (sessionRequests === 1) throw new TypeError('Failed to fetch')
        return envelope(sessionPayload())
      }
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    }))

    render(<App />)

    expect(await screen.findByLabelText('正在重新验证登录状态')).toBeInTheDocument()
    window.dispatchEvent(new Event('online'))
    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    expect(sessionRequests).toBe(2)
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).not.toBeNull()
  })

  it('silently leaves the workspace when the fixed deadline is reached', async () => {
    const expiresAt = new Date(Date.now() + 1000).toISOString()
    seedSession('expiring-token', expiresAt)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload(expiresAt))
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    }))

    render(<App />)

    expect(await screen.findByLabelText('对话内容')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '欢迎回来' }, { timeout: 2500 }))
      .toBeInTheDocument()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('silently unmounts an active workspace when an authenticated request returns 401', async () => {
    seedSession()
    window.history.replaceState(null, '', '/?thread=private-thread')
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope(null, 401, 1_001_001_000, '登录已过期')
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('shows the global 300ms transition only after a manual login succeeds', async () => {
    const browserUser = userEvent.setup()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/auth/login')) return envelope(loginPayload())
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) return envelope({ items: [], nextCursor: null })
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByLabelText('正在进入工作区')).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument(), { timeout: 1500 })
  })

  it('shows login service failures only through the global toast', async () => {
    const browserUser = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      if (new URL(request.url).pathname.endsWith('/api/auth/login')) {
        return new Response(null, { status: 503 })
      }
      throw new Error(`unexpected request: ${request.url}`)
    }))

    render(<App />)
    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/^服务暂不可用，请稍后重试$/)
    expect(screen.getByRole('alert').closest('.toast-card')).not.toBeNull()
    expect(document.querySelector('.auth-form-error')).toBeNull()
    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
  })

  it('keeps rejected credentials inline without creating a global toast', async () => {
    const browserUser = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      if (new URL(request.url).pathname.endsWith('/api/auth/login')) {
        return envelope(null, 401, 1_001_001_000, '用户名或密码错误')
      }
      throw new Error(`unexpected request: ${request.url}`)
    }))

    render(<App />)
    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'wrong-password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('用户名或密码错误')
    expect(screen.getByRole('alert')).toHaveClass('auth-form-error')
    expect(document.querySelector('.toast-card')).toBeNull()
  })

  it('rejects login atomically when the browser cannot persist the session', async () => {
    const browserUser = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      if (new URL(request.url).pathname.endsWith('/api/auth/login')) {
        return envelope(loginPayload())
      }
      throw new Error(`unexpected request: ${request.url}`)
    }))
    render(<App />)
    vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('storage disabled', 'SecurityError')
    })

    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '浏览器无法保存登录状态，请检查隐私或存储设置后重试',
    )
    expect(screen.getByRole('alert')).toHaveClass('auth-form-error')
    expect(screen.queryByLabelText('正在检查登录状态')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(AUTH_SESSION_STORAGE_KEY)).toBeNull()
  })

  it('keeps the workspace and transition unmounted while a fresh login is verified by /me', async () => {
    const browserUser = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/auth/login')) return envelope(loginPayload('pending-token'))
      if (url.pathname.endsWith('/api/auth/me')) {
        return new Promise<Response>(() => undefined)
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    }))

    render(<App />)
    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    expect(screen.getByLabelText('正在检查登录状态')).toBeInTheDocument()
    expect(screen.queryByLabelText('正在进入工作区')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
  })

  it('skips the transition and delay when reduced motion is requested', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    })))
    const browserUser = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/auth/login')) return envelope(loginPayload())
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) return envelope({ items: [], nextCursor: null })
      throw new Error(`unexpected request: ${url.pathname}`)
    }))

    render(<App />)
    await browserUser.type(screen.getByLabelText('用户名'), 'yunsan')
    await browserUser.type(screen.getByLabelText('密码'), 'password')
    await browserUser.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    expect(screen.queryByLabelText('正在进入工作区')).not.toBeInTheDocument()
  })

  it('calls the real logout endpoint, clears the workspace, and removes the thread route', async () => {
    seedSession()
    window.history.replaceState(null, '', '/?thread=private-thread')
    const browserUser = userEvent.setup()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/auth/logout')) return envelope(null)
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) return envelope({ items: [], nextCursor: null })
      if (url.pathname.endsWith('/api/conversation/private-thread/history')) {
        return envelope({
          id: 1,
          threadId: 'private-thread',
          title: '私密会话',
          lastModel: 'main',
          asOfSeq: 1,
          headRunId: 'private-run',
          availableHeads: ['private-run'],
          historyCursor: null,
          messageCount: 0,
          toolCallCount: 0,
          pinned: false,
          messages: [],
          reasoning: [],
          graph: emptyTraceGraph(1),
          state: { root: {}, subgraphs: {} },
          interactions: [],
          status: { execution: 'succeeded', headRunId: 'private-run' },
          completeness: {
            missingPrefix: false,
            missingTail: false,
            payloadOmitted: false,
          },
          createdAt: '2026-08-10T00:00:00.000Z',
          updatedAt: '2026-08-10T00:00:00.000Z',
        })
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    writeActiveRunSession({
      threadId: 'other-private-thread',
      payload: {
        threadId: 'other-private-thread',
        runId: 'private-run',
        state: {},
        messages: [{ id: 'request-private-run', role: 'user', content: 'private prompt' }],
        tools: [],
        context: [],
        forwardedProps: { model: 'main', command: { plan: 'off' } },
      },
      mode: 'start',
      lastSeq: 7,
    })
    expect(readActiveRunSession()).not.toBeNull()
    await browserUser.click(screen.getByRole('button', { name: '打开用户菜单' }))
    await browserUser.click(screen.getByRole('menuitem', { name: '退出登录' }))

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.queryByLabelText('对话内容')).not.toBeInTheDocument()
    expect(window.location.search).toBe('')
    expect(readActiveRunSession()).toBeNull()
    expect(fetchMock.mock.calls.some(([input]) => {
      const request = input instanceof Request ? input : new Request(input)
      return new URL(request.url).pathname.endsWith('/api/auth/logout') && request.method === 'POST'
    })).toBe(true)
  })

  it('returns to login immediately while logout revocation continues in the background', async () => {
    seedSession()
    const browserUser = userEvent.setup()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input)
      const url = new URL(request.url)
      if (url.pathname.endsWith('/api/auth/me')) return envelope(sessionPayload())
      if (url.pathname.endsWith('/api/conversation/config')) return envelope({ dayRanges: [7, 30] })
      if (url.pathname.endsWith('/api/conversation/history')) {
        return envelope({ items: [], nextCursor: null })
      }
      if (url.pathname.endsWith('/api/auth/logout')) {
        return new Promise<Response>(() => undefined)
      }
      throw new Error(`unexpected request: ${url.pathname}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await waitFor(() => expect(screen.getByLabelText('对话内容')).toBeInTheDocument())
    await browserUser.click(screen.getByRole('button', { name: '打开用户菜单' }))
    await browserUser.click(screen.getByRole('menuitem', { name: '退出登录' }))

    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    const logoutRequest = fetchMock.mock.calls
      .map(([input]) => input instanceof Request ? input : new Request(input))
      .find((request) => new URL(request.url).pathname.endsWith('/api/auth/logout'))
    expect(logoutRequest?.headers.get('Authorization')).toBe('Bearer token-123')
  })
})
