import axios, {
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

import {
  clearAuthSession,
  getAuthorizationHeader,
  notifyAuthFailure,
} from '../../auth/session'
import { API_BASE_URL, buildApiUrl } from './config'
import { GLOBAL_ERROR_CODES } from './errorCodes'

interface ApiEnvelope<T> {
  code: number
  message: string
  data: T | null
}

interface ErrorPolicy {
  suppressGlobalError?: boolean
  suppressAuthFailure?: boolean
  authorization?: string | null
}

export interface ApiAxiosRequestConfig<D = unknown> extends AxiosRequestConfig<D>, ErrorPolicy {
  requiresAuth?: boolean
}

interface InternalApiAxiosRequestConfig<D = unknown>
  extends InternalAxiosRequestConfig<D>, ErrorPolicy {
  requiresAuth?: boolean
}

export interface RequestOptions extends ErrorPolicy {
  method?: string
  body?: BodyInit | object | null
  headers?: HeadersInit
  signal?: AbortSignal
  requiresAuth?: boolean
  credentials?: RequestCredentials
}

type ApiErrorListener = (error: ApiError) => void

const apiErrorListeners = new Set<ApiErrorListener>()
const finalizedErrors = new WeakSet<ApiError>()

export class ApiError extends Error {
  code: number
  status: number
  isAuthError: boolean

  constructor(message: string, options?: { code?: number; status?: number; isAuthError?: boolean }) {
    super(message)
    this.name = 'ApiError'
    this.code = options?.code ?? GLOBAL_ERROR_CODES.internalServerError
    this.status = options?.status ?? 200
    this.isAuthError = options?.isAuthError ?? false
  }
}

export class AuthError extends ApiError {
  constructor(message: string, options?: { code?: number; status?: number }) {
    super(message, { ...options, isAuthError: true })
    this.name = 'AuthError'
  }
}

function isApiEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<ApiEnvelope<unknown>>
  return typeof candidate.code === 'number'
    && typeof candidate.message === 'string'
    && 'data' in candidate
}

function isAuthFailure(status: number) {
  return status === GLOBAL_ERROR_CODES.unauthorized
}

function emitApiError(error: ApiError, policy: ErrorPolicy) {
  if (error.isAuthError || policy.suppressGlobalError) return
  if (policy.authorization && getAuthorizationHeader() !== policy.authorization) return
  for (const listener of apiErrorListeners) listener(error)
}

function handleAuthFailure(error: ApiError, policy: ErrorPolicy) {
  if (!error.isAuthError) return
  const currentAuthorization = getAuthorizationHeader()
  const isCurrentSession = policy.authorization
    ? currentAuthorization === policy.authorization
    : currentAuthorization === null
  if (!isCurrentSession) return
  clearAuthSession()
  if (!policy.suppressAuthFailure) {
    notifyAuthFailure({
      code: error.code,
      message: error.message || '登录已失效，请重新登录。',
    })
  }
}

function finalizeError(error: ApiError, policy: ErrorPolicy) {
  if (finalizedErrors.has(error)) return error
  finalizedErrors.add(error)
  handleAuthFailure(error, policy)
  emitApiError(error, policy)
  return error
}

function apiErrorFromEnvelope(
  payload: ApiEnvelope<unknown>,
  status: number,
  policy: ErrorPolicy,
) {
  const ErrorType = isAuthFailure(status) ? AuthError : ApiError
  return finalizeError(
    new ErrorType(payload.message || '请求处理失败', {
      code: payload.code,
      status,
    }),
    policy,
  )
}

function unwrapApiEnvelope<T>(payload: unknown, status: number, policy: ErrorPolicy): T {
  if (!isApiEnvelope(payload) || payload.code !== GLOBAL_ERROR_CODES.success) {
    throw finalizeError(
      new ApiError('接口返回格式不合法', {
        code: GLOBAL_ERROR_CODES.internalServerError,
        status,
      }),
      policy,
    )
  }

  return payload.data as T
}

function readConfigAuthorization(config?: ApiAxiosRequestConfig): string | null {
  const headers = config?.headers
  if (!headers) return null
  const getter = (headers as { get?: (name: string) => unknown }).get
  const value = typeof getter === 'function'
    ? getter.call(headers, 'Authorization')
    : (headers as Record<string, unknown>).Authorization
      ?? (headers as Record<string, unknown>).authorization
  return typeof value === 'string' ? value : null
}

function requestPolicy(config?: ApiAxiosRequestConfig): ErrorPolicy {
  return {
    suppressGlobalError: config?.suppressGlobalError,
    suppressAuthFailure: config?.suppressAuthFailure,
    authorization: readConfigAuthorization(config),
  }
}

function transportErrorMessage(status: number) {
  if (status === GLOBAL_ERROR_CODES.unauthorized) return '请先登录'
  if (status === GLOBAL_ERROR_CODES.forbidden) return '没有该操作权限'
  if (status === 404) return '请求未找到'
  if (status === 429) return '请求过于频繁，请稍后重试'
  if (status >= 500) return '服务暂不可用，请稍后重试。'
  return `请求失败 (${status})`
}

const currentOrigin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin
const apiBaseUrl = API_BASE_URL
  ? `${currentOrigin}${API_BASE_URL.startsWith('/') ? API_BASE_URL : `/${API_BASE_URL}`}`
  : currentOrigin

export const apiClient = axios.create({
  adapter: 'fetch',
  baseURL: /^https?:\/\//i.test(API_BASE_URL) ? API_BASE_URL : apiBaseUrl,
  // Axios 默认缓存模块加载时的 fetch；间接调用可让测试替身和运行时补丁生效
  env: { fetch: (input, init) => globalThis.fetch(input, init) },
})

apiClient.interceptors.request.use((config) => {
  const apiConfig = config as InternalApiAxiosRequestConfig
  config.headers.set('Accept', 'application/json')
  if (apiConfig.requiresAuth === false || config.headers.has('Authorization')) return config

  const authorization = getAuthorizationHeader()
  if (!authorization) {
    return Promise.reject(finalizeError(
      new AuthError('请先登录', {
        code: GLOBAL_ERROR_CODES.unauthorized,
        status: GLOBAL_ERROR_CODES.unauthorized,
      }),
      requestPolicy(apiConfig),
    ))
  }
  config.headers.set('Authorization', authorization)
  return config
})

apiClient.interceptors.response.use(
  (response: AxiosResponse<unknown>) => {
    const policy = requestPolicy(response.config as ApiAxiosRequestConfig)
    if (response.status === 204) {
      response.data = undefined
      return response
    }
    response.data = unwrapApiEnvelope(response.data, response.status, policy)
    return response
  },
  (reason: unknown) => {
    if (reason instanceof ApiError || axios.isCancel(reason)) {
      return Promise.reject(reason)
    }

    if (!axios.isAxiosError(reason)) {
      return Promise.reject(finalizeError(
        new ApiError('网络请求失败，请稍后重试。', { status: 0 }),
        {},
      ))
    }

    const status = reason.response?.status ?? 0
    const policy = requestPolicy(reason.config as ApiAxiosRequestConfig | undefined)
    if (isApiEnvelope(reason.response?.data)
      && reason.response.data.code !== GLOBAL_ERROR_CODES.success) {
      return Promise.reject(apiErrorFromEnvelope(reason.response.data, status, policy))
    }

    if (status === 0) {
      return Promise.reject(finalizeError(
        new ApiError('网络请求失败，请稍后重试。', { status: 0 }),
        policy,
      ))
    }

    const ErrorType = isAuthFailure(status) ? AuthError : ApiError
    return Promise.reject(finalizeError(
      new ErrorType(transportErrorMessage(status), {
        code: status,
        status,
      }),
      policy,
    ))
  },
)

function normalizeRequestBody(body: RequestOptions['body']) {
  if (body == null) return { body: undefined, contentType: null }
  if (typeof FormData !== 'undefined' && body instanceof FormData) return { body, contentType: null }
  if ((typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams)
    || (typeof Blob !== 'undefined' && body instanceof Blob)
    || body instanceof ArrayBuffer) {
    return { body, contentType: null }
  }
  if (typeof body === 'string') return { body, contentType: 'application/json' }
  return {
    body: JSON.stringify(body),
    contentType: 'application/json',
  }
}

function buildStreamHeaders(options: RequestOptions, contentType: string | null) {
  const headers = new Headers(options.headers)
  headers.set('Accept', 'text/event-stream')
  if (contentType && !headers.has('Content-Type')) headers.set('Content-Type', contentType)
  if (options.requiresAuth !== false) {
    const authorization = getAuthorizationHeader()
    if (!authorization) {
      throw finalizeError(
        new AuthError('请先登录', {
          code: GLOBAL_ERROR_CODES.unauthorized,
          status: GLOBAL_ERROR_CODES.unauthorized,
        }),
        options,
      )
    }
    headers.set('Authorization', authorization)
  }
  return headers
}

async function fetchStreamResponse(path: string, options: RequestOptions) {
  const { body, contentType } = normalizeRequestBody(options.body)
  const headers = buildStreamHeaders(options, contentType)

  try {
    const response = await fetch(buildApiUrl(path), {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
      credentials: options.credentials,
    })
    return { response, authorization: headers.get('Authorization') }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw finalizeError(
      new ApiError('网络请求失败，请稍后重试。', { status: 0 }),
      options,
    )
  }
}

async function parseJsonPayload(response: Response, policy: ErrorPolicy) {
  try {
    return await response.json()
  } catch {
    throw finalizeError(
      new ApiError('接口返回的 JSON 无法解析', {
        code: GLOBAL_ERROR_CODES.internalServerError,
        status: response.status,
      }),
      policy,
    )
  }
}

async function cancelUnreadBody(response: Response): Promise<void> {
  await response.body?.cancel().catch(() => undefined)
}

async function buildStreamHttpError(response: Response, policy: ErrorPolicy): Promise<never> {
  await cancelUnreadBody(response)
  const ErrorType = isAuthFailure(response.status) ? AuthError : ApiError
  throw finalizeError(
    new ErrorType(transportErrorMessage(response.status), {
      code: response.status || GLOBAL_ERROR_CODES.internalServerError,
      status: response.status,
    }),
    policy,
  )
}

export function subscribeApiErrors(listener: ApiErrorListener) {
  apiErrorListeners.add(listener)
  return () => {
    apiErrorListeners.delete(listener)
  }
}

export async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = Object.fromEntries(new Headers(options.headers).entries())
  const config: ApiAxiosRequestConfig = {
    url: path,
    method: options.method ?? 'GET',
    data: options.body ?? undefined,
    headers,
    signal: options.signal,
    withCredentials: options.credentials === 'include'
      ? true
      : options.credentials === 'omit'
        ? false
        : undefined,
    requiresAuth: options.requiresAuth,
    suppressGlobalError: options.suppressGlobalError,
    suppressAuthFailure: options.suppressAuthFailure,
  }
  const response = await apiClient.request<T>(config)
  return response.data
}

export async function requestEventStream(path: string, options: RequestOptions = {}): Promise<Response> {
  const { response, authorization } = await fetchStreamResponse(path, options)
  const policy = { ...options, authorization }
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''

  if (contentType.includes('text/event-stream')) {
    if (!response.ok) return buildStreamHttpError(response, policy)
    return response
  }

  if (contentType.includes('application/json')) {
    const payload = await parseJsonPayload(response, policy)
    if (!response.ok) {
      if (isApiEnvelope(payload) && payload.code !== GLOBAL_ERROR_CODES.success) {
        throw apiErrorFromEnvelope(payload, response.status, policy)
      }
      return buildStreamHttpError(response, policy)
    }
    throw finalizeError(
      new ApiError('聊天接口返回了非流式成功响应', {
        code: response.status || GLOBAL_ERROR_CODES.internalServerError,
        status: response.status,
      }),
      policy,
    )
  }

  if (!response.ok) return buildStreamHttpError(response, policy)

  await cancelUnreadBody(response)
  throw finalizeError(
    new ApiError('聊天接口返回了不支持的响应类型', {
      code: response.status || GLOBAL_ERROR_CODES.internalServerError,
      status: response.status,
    }),
    policy,
  )
}
