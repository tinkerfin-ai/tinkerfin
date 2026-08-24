import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ui'
import { GlobalErrorFallback } from './components/ui/GlobalErrorFallback'
import '@fontsource-variable/inter'
import '@fontsource-variable/inter/wght-italic.css'
import '@fontsource-variable/noto-sans-sc'
import '@fontsource-variable/jetbrains-mono'
import './styles/tokens.css'
import './styles/typography.css'
import './styles/global.css'
import './components/ui/ui.css'
import { normalizeAppLocation } from './lib/threadRoute'
import { LocaleProvider } from './i18n'

normalizeAppLocation()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <LocaleProvider>
      <ErrorBoundary
        fallback={() => <GlobalErrorFallback />}
        onError={(error) => console.error('TinkerFin 页面渲染失败', error)}
      >
        <App />
      </ErrorBoundary>
    </LocaleProvider>
  </StrictMode>,
)
