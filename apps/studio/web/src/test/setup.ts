import '@testing-library/jest-dom/vitest'
import { beforeEach, vi } from 'vitest'

const memory = new Map<string, string>()
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => memory.get(key) ?? null,
    setItem: (key: string, value: string) => memory.set(key, String(value)),
    removeItem: (key: string) => memory.delete(key),
    clear: () => memory.clear(),
    key: (index: number) => [...memory.keys()][index] ?? null,
    get length() { return memory.size },
  },
})

Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: (query: string) => {
    const minWidth = query.match(/min-width:\s*(\d+)px/)?.[1]
    const maxWidth = query.match(/max-width:\s*(\d+)px/)?.[1]
    const matches = query.includes('prefers-')
      ? false
      : (!minWidth || window.innerWidth >= Number(minWidth))
        && (!maxWidth || window.innerWidth <= Number(maxWidth))
    return {
    matches,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  }
  },
})

Object.defineProperty(globalThis, 'ResizeObserver', {
  configurable: true,
  value: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
})

beforeEach(() => {
  window.localStorage.clear()
})
