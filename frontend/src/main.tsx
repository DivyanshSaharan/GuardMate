import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { PublicSite } from './website/PublicSite'
import './styles.css'

const runtime = await fetch('/api/runtime')
  .then((response) => (response.ok ? response.json() : { mode: 'local' }))
  .catch(() => ({ mode: 'local' }))

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {runtime.mode === 'cloud' ? (
      <PublicSite />
    ) : (
      <App desktop={runtime.mode === 'desktop'} />
    )}
  </React.StrictMode>,
)
