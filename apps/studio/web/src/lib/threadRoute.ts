/**
 * 极简「会话 URL 路由」：用 `?thread={threadId}` 查询参数记录当前会话，
 * 让浏览器刷新后能回到原会话（页面无 react-router，也无工作区持久化）
 *
 * 用 `history.replaceState` 同步而非 `pushState`——只保证 URL 始终反映当前
 * 会话、刷新可还原，不在会话间产生历史条目，避免「后退键在会话间跳转」
 * 触发实时流断开确认的复杂度
 */

const THREAD_PARAM = 'thread'
const APP_PATHNAME = '/'
const LAST_VALID_LOCATION_KEY = 'tinkerfin:last-valid-location'

function relativeLocation(url: URL): string {
  return `${url.pathname}${url.search}${url.hash}`
}

function readStoredValidLocation(): string | null {
  try {
    const storedLocation = window.sessionStorage.getItem(LAST_VALID_LOCATION_KEY)
    if (!storedLocation) return null
    const url = new URL(storedLocation, window.location.origin)
    if (url.origin !== window.location.origin || url.pathname !== APP_PATHNAME) return null
    return relativeLocation(url)
  } catch {
    return null
  }
}

function rememberCurrentLocation(): void {
  try {
    window.sessionStorage.setItem(
      LAST_VALID_LOCATION_KEY,
      window.location.pathname + window.location.search + window.location.hash,
    )
  } catch {
    // 浏览器禁用会话存储时仍可回退到应用根路径
  }
}

/** 在应用挂载前把未知路径替换为最近一次有效的应用地址 */
export function normalizeAppLocation(): void {
  if (window.location.pathname === APP_PATHNAME) {
    rememberCurrentLocation()
    return
  }

  const desired = readStoredValidLocation() ?? APP_PATHNAME
  window.history.replaceState(null, '', desired)
}

/** 从当前 URL 读取 `?thread=` 指定的会话 id，无则返回空串 */
export function readThreadFromLocation(): string {
  return new URLSearchParams(window.location.search).get(THREAD_PARAM) ?? ''
}

/** 把会话 id 写进 `?thread=`（空串则删除参数），与当前 URL 相同时不重复 replaceState */
export function writeThreadToLocation(threadId: string): void {
  const url = new URL(window.location.href)
  url.pathname = APP_PATHNAME
  if (threadId) url.searchParams.set(THREAD_PARAM, threadId)
  else url.searchParams.delete(THREAD_PARAM)
  // url.toString() 含 origin，去掉以得到 pathname+search+hash
  const desired = url.toString().slice(url.origin.length)
  const current = window.location.pathname + window.location.search + window.location.hash
  if (desired !== current) {
    window.history.replaceState(null, '', desired)
  }
  rememberCurrentLocation()
}
