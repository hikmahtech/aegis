import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// Registering a service worker is what makes Chrome offer "Install app" on
// Android. sw.js deliberately has no fetch handler — see the warning in it.
// Dev is skipped: vite serves from src/, so there is no /sw.js to register.
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Installability is a nice-to-have; a failed registration must never
      // take the admin panel down with it.
    })
  })
}
