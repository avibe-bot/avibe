import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

import { connect } from './connection'
import './styles.css'

/**
 * The frozen desktop bootstrap contract.
 *
 * Mirrors `BootstrapStatus` in `runtime-host/src/status.rs`. The Rust side owns
 * every value here — this page renders it and never decides anything itself,
 * least of all when to navigate.
 */
type BootstrapPhase = 'probing' | 'starting' | 'ready' | 'failed'
type NoticeCode =
  | 'preparing'
  | 'probing'
  | 'adopted'
  | 'starting'
  | 'ready'
  | 'invalid_origin'
  | 'runtime_not_found'
  | 'runtime_install_failed'
  | 'runtime_discovery_failed'
  | 'runtime_spawn_failed'
  | 'runtime_stopped'
  | 'runtime_ownership_lost'
  | 'runtime_stop_failed'
  | 'launcher_exited'
  | 'ready_timeout'
  | 'workbench_navigation_failed'

interface BootstrapNotice {
  code: string
  seconds?: number
}

interface BootstrapStatus {
  phase: BootstrapPhase
  origin: string
  attempt: number
  notice: BootstrapNotice
  retryable: boolean
}

interface BootstrapCatalog {
  retry: string
  installationHelp: string
  notRunningTitle: string
  shellUnavailable: string
  slowStart: string
  genericFailure: string
  notices: Record<NoticeCode, string>
}

declare const __DESKTOP_BOOTSTRAP_CATALOGS__: Record<'en' | 'zh', BootstrapCatalog>

const STATUS_EVENT = 'bootstrap-status'
const NOTICE_CODES: ReadonlySet<string> = new Set<NoticeCode>([
  'preparing',
  'probing',
  'adopted',
  'starting',
  'ready',
  'invalid_origin',
  'runtime_not_found',
  'runtime_install_failed',
  'runtime_discovery_failed',
  'runtime_spawn_failed',
  'runtime_stopped',
  'runtime_ownership_lost',
  'runtime_stop_failed',
  'launcher_exited',
  'ready_timeout',
  'workbench_navigation_failed',
])

const root = document.documentElement
const messageEl = requireElement('message')
const originEl = requireElement('origin')
const trackEl = requireElement('track')
const actionsEl = requireElement('actions')
const hintEl = requireElement('hint')
const retryEl = requireElement<HTMLButtonElement>('retry')
const helpEl = requireElement<HTMLButtonElement>('help')
const locale = selectLocale(navigator.languages)
const catalog = __DESKTOP_BOOTSTRAP_CATALOGS__[locale]

/** When a reader of a still-starting window may start to wonder. */
const SLOW_START_MS = 15_000

let sawEvent = false
let slow = false
// The page shows "not responding", not a status from the app.
let unavailable = false

root.lang = locale
messageEl.textContent = catalog.notices.preparing
hintEl.textContent = catalog.slowStart
retryEl.textContent = catalog.retry
helpEl.textContent = catalog.installationHelp
let slowTimer = 0
startSlowClock()

/** Times the wait from the start of this attempt: page load, or a new start after a failure. */
function startSlowClock(): void {
  window.clearTimeout(slowTimer)
  slow = false
  slowTimer = window.setTimeout(() => {
    slow = true
    showSlowHint()
  }, SLOW_START_MS)
}

function requireElement<T extends HTMLElement = HTMLElement>(id: string): T {
  const element = document.getElementById(id)
  if (!element) {
    throw new Error(`bootstrap markup is missing #${id}`)
  }
  return element as T
}

function render(status: BootstrapStatus): void {
  unavailable = false
  if (root.dataset.phase === 'failed' && status.phase !== 'failed') {
    startSlowClock()
  }
  root.dataset.phase = status.phase
  messageEl.textContent = localizeNotice(status.notice)
  originEl.textContent = status.origin

  // The Runtime is either coming up or it is not; a determinate bar would be a
  // lie, and a bar after failure would suggest work is still happening.
  trackEl.hidden = status.phase === 'failed'

  const showRetry = status.phase === 'failed' && status.retryable
  const showHelp = status.phase === 'failed' && showInstallHelp(status.notice)
  retryEl.disabled = false
  retryEl.hidden = !showRetry
  helpEl.hidden = !showHelp
  actionsEl.hidden = !showRetry && !showHelp
  document.title = status.phase === 'failed' ? catalog.notRunningTitle : 'Avibe'
  showSlowHint()
}

/** The second line appears only while the app is still starting, and only once the wait is long. */
function showSlowHint(): void {
  const phase = root.dataset.phase
  hintEl.hidden = !slow || phase === 'failed' || phase === 'ready'
}

/** The page's state before the first status arrives: the app is starting. */
function renderStarting(): void {
  unavailable = false
  root.dataset.phase = 'probing'
  messageEl.textContent = catalog.notices.preparing
  originEl.textContent = ''
  trackEl.hidden = false
  actionsEl.hidden = true
  document.title = 'Avibe'
  showSlowHint()
}

function selectLocale(languages: readonly string[]): 'en' | 'zh' {
  for (const language of languages) {
    const normalized = language.toLowerCase()
    if (normalized.startsWith('zh')) {
      return 'zh'
    }
    if (normalized.startsWith('en')) {
      return 'en'
    }
  }
  return 'en'
}

function localizeNotice(notice: BootstrapNotice): string {
  if (!NOTICE_CODES.has(notice.code)) {
    return catalog.genericFailure
  }
  const code = notice.code as NoticeCode
  const template = catalog.notices[code]
  if (code !== 'ready_timeout') {
    return template
  }
  if (!Number.isSafeInteger(notice.seconds) || (notice.seconds ?? -1) < 0) {
    return catalog.genericFailure
  }
  return template.replace('{{seconds}}', String(notice.seconds))
}

function showInstallHelp(notice: BootstrapNotice): boolean {
  return (
    notice.code === 'runtime_not_found' ||
    notice.code === 'runtime_discovery_failed' ||
    notice.code === 'launcher_exited'
  )
}

retryEl.addEventListener('click', () => {
  // Disable synchronously so a double click cannot queue two runs. The native
  // command reports whether it acquired bootstrap ownership; a rejected retry
  // stays visible instead of waiting forever for an event that will not arrive.
  retryEl.disabled = true
  void invoke<boolean>('bootstrap_retry')
    .then((scheduled) => {
      // Once scheduled, status events own both visibility and enabled state.
      // A fast terminal event may already have rendered Retry again before this
      // promise resolves, so the response must not overwrite that newer state.
      if (!scheduled) {
        retryEl.disabled = false
      }
    })
    .catch((error: unknown) => {
      // The app answered a moment ago with the failure on screen; one refused
      // call does not make it unresponsive. Keep that failure and its actions.
      console.error('bootstrap retry failed', error)
      retryEl.disabled = false
    })
})

helpEl.addEventListener('click', () => {
  void invoke('open_install_docs').catch((error: unknown) => {
    console.error('installation docs could not be opened', error)
  })
})

function start(): Promise<void> {
  return connect({
    // Subscribe before asking for the current value, so a status published
    // while this page was still loading cannot be missed.
    subscribe: () =>
      listen<BootstrapStatus>(STATUS_EVENT, (event) => {
        sawEvent = true
        render(event.payload)
      }),
    snapshot: async () => {
      const current = await invoke<BootstrapStatus | null>('bootstrap_status')
      // If a newer event arrived before the snapshot resolved, keep that newer
      // state. The snapshot exists only to populate a page that loaded mid-run.
      if (current && !sawEvent) {
        render(current)
      }
    },
    heardFromApp: () => sawEvent,
    onStuck: reportUnavailable,
    onRecovered: () => {
      // A status may already have replaced the message; if not, the app has
      // answered without one yet, so it is starting.
      if (unavailable) {
        renderStarting()
      }
    },
  })
}

/**
 * The native side is the only thing that can answer these calls. When it has
 * not answered for a long time, the page says so plainly instead of sitting on
 * a spinner forever.
 */
function reportUnavailable(error: unknown): void {
  console.error('bootstrap command failed', error)
  unavailable = true
  root.dataset.phase = 'failed'
  messageEl.textContent = catalog.shellUnavailable
  trackEl.hidden = true
  actionsEl.hidden = true
  document.title = catalog.notRunningTitle
  showSlowHint()
}

void start()
