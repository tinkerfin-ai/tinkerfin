import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import '@fontsource-variable/geist'
import '@fontsource-variable/noto-sans-sc'
import '@fontsource-variable/geist-mono'
import './styles/typography.css'
import './styles/global.css'
import { normalizeAppLocation } from './lib/threadRoute'

normalizeAppLocation()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
