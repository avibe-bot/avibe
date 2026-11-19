import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { i18nReady } from './i18n/app'
import './index.css'
import App from './App.tsx'
import { declareReplayedShell } from './lib/bootSession'
import { isNativeShell } from './lib/native'

// Synchronously, before the document finishes loading: the phone app reads it
// to decide whether this page, shown from its cache, may be uncovered.
declareReplayedShell(isNativeShell())

// The first frame draws in the reader's language: its copy is a chunk that
// index.html already started downloading next to this one.
void i18nReady.then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
})
