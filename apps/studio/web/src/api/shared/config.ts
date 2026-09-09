const normalizeBaseUrl = (value: string | undefined) => {
  const trimmed = value?.trim()
  if (!trimmed) return ''
  const withoutTrailingSlash = trimmed.endsWith('/') ? trimmed.slice(0, -1) : trimmed
  if (/^https?:\/\//i.test(withoutTrailingSlash)) return withoutTrailingSlash
  return withoutTrailingSlash.startsWith('/')
    ? withoutTrailingSlash
    : `/${withoutTrailingSlash}`
}

export const API_BASE_URL = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL)

export const buildApiUrl = (path: string) => (
  /^https?:\/\//i.test(path)
    ? path
    : API_BASE_URL
      ? `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
      : path
)
