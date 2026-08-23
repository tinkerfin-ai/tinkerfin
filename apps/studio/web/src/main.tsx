import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { Button, ErrorBoundary } from './components/ui'
import '@fontsource-variable/inter'
import '@fontsource-variable/inter/wght-italic.css'
import '@fontsource-variable/noto-sans-sc'
import '@fontsource-variable/jetbrains-mono'
import './styles/tokens.css'
import './styles/typography.css'
import './styles/global.css'
import './components/ui/ui.css'
import { normalizeAppLocation } from './lib/threadRoute'

normalizeAppLocation()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary
      fallback={() => (
        <main id="main-content" className="app-error-fallback" role="alert">
          <div>
            <h1>页面暂时无法显示</h1>
            <p>界面渲染遇到问题，请重新加载后再试。</p>
            <Button variant="primary" onClick={() => window.location.reload()}>重新加载页面</Button>
          </div>
        </main>
      )}
      onError={(error) => console.error('TinkerFin 页面渲染失败', error)}
    >
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
