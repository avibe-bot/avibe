import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

import './styles.css'

/**
 * The frozen desktop bootstrap contract.
 *
 * Mirrors `BootstrapStatus` in `runtime-host/src/status.rs`; see
 * `docs/plans/tauri-desktop-vertical-slice.md`. The Rust side owns every value
 * here — this page renders it and never decides anything itself, least of all
 * when to navigate.
 */
type BootstrapPhase = 'probing' | 'starting' | 'ready' | 'failed'

interface BootstrapStatus {
  phase: BootstrapPhase
  origin: string
  attempt: number
  message: string
  retryable: boolean
}

const STATUS_EVENT = 'bootstrap-status'

const root = document.documentElement
const messageEl = requireElement('message')
const originEl = requireElement('origin')
const trackEl = requireElement('track')
const actionsEl = requireElement('actions')
const retryEl = requireElement<HTMLButtonElement>('retry')
const helpEl = requireElement<HTMLButtonElement>('help')

function requireElement<T extends HTMLElement = HTMLElement>(id: string): T {
  const element = document.getElementById(id)
  if (!element) {
    throw new Error(`bootstrap markup is missing #${id}`)
  }
  return element as T
}

function render(status: BootstrapStatus): void {
  root.dataset.phase = status.phase
  messageEl.textContent = status.message
  originEl.textContent = status.origin

  // The Runtime is either coming up or it is not; a determinate bar would be a
  // lie, and a bar after failure would suggest work is still happening.
  trackEl.hidden = status.phase === 'failed'

  actionsEl.hidden = !(status.phase === 'failed' && status.retryable)
  document.title = status.phase === 'failed' ? 'Avibe — not running' : 'Avibe'
}

retryEl.addEventListener('click', () => {
  // Optimistically disable so a double click cannot queue two runs; the next
  // status event decides whether the button comes back.
  retryEl.hidden = true
  void invoke('bootstrap_retry').catch(reportUnavailable)
})

helpEl.addEventListener('click', () => {
  void invoke('open_install_docs').catch((error: unknown) => {
    console.error('installation docs could not be opened', error)
  })
})

async function start(): Promise<void> {
  // Subscribe before asking for the current value, so a status published while
  // this page was still loading cannot be missed.
  await listen<BootstrapStatus>(STATUS_EVENT, (event) => render(event.payload))

  const current = await invoke<BootstrapStatus | null>('bootstrap_status')
  if (current) {
    render(current)
  }
}

/**
 * The shell is the only thing that can answer these calls. If it cannot, the
 * page says so plainly instead of sitting on a spinner forever.
 */
function reportUnavailable(error: unknown): void {
  console.error('bootstrap command failed', error)
  render({
    phase: 'failed',
    origin: originEl.textContent ?? '',
    attempt: 0,
    message: 'The Avibe desktop shell stopped responding. Quit and open Avibe again.',
    retryable: false,
  })
}

void start().catch(reportUnavailable)
