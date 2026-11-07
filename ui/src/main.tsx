import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { i18nReady } from './i18n/app'
import './index.css'
import App from './App.tsx'

// The first frame draws in the reader's language: its copy is a chunk that
// index.html already started downloading next to this one.
void i18nReady.then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
})
