// Idle-time prefetch of the conversations a reader is most likely to open next
// (docs/plans/web-ui-local-cache.md section 4.2): the few most recent Inbox
// Sessions that have no stored snapshot get the same bootstrap the chat page
// reads, so the first tap on each paints at once too.
//
// What it must not do: act as the reader. The read is the bootstrap GET, which
// marks nothing read (mark-read is a separate POST only an open chat makes), and
// it is declared a prefetch, so it does not renew the session either. It runs
// only while the page is visible, in idle time, at most three at a time, and
// never on a connection the Network Information API reports as metered or
// data-saving. Every failure is silent; a 403 or 404 deletes that record.

import type { WorkbenchSessionBootstrap } from '../context/ApiContext';
import { apiFetch } from './apiFetch';
import {
  beginChatSnapshotRequest,
  chatSnapshotFromBootstrap,
  chatSnapshotOwnerForAnswer,
  forgetChatSnapshot,
  readChatSnapshot,
  writeChatSnapshot,
} from './chatSnapshots';
import { getLocalSnapshotPrincipal } from './localSnapshots';

/** How many of the most recent Inbox Sessions are considered. */
export const CHAT_PREFETCH_CANDIDATES = 5;
/** Bootstraps in flight at once. */
const CHAT_PREFETCH_CONCURRENCY = 3;
const IDLE_TIMEOUT_MS = 4000;
const IDLE_FALLBACK_MS = 1500;

/** The chat page's bootstrap GET, declared a prefetch so it renews no session.
 *  Nothing else happens on the way: no draft bookkeeping, no error toast, no
 *  read cache. It rejects with the response status on a failure. */
async function fetchBootstrapForPrefetch(sessionId: string): Promise<WorkbenchSessionBootstrap> {
  const path = `/api/sessions/${encodeURIComponent(sessionId)}/bootstrap`;
  const response = await apiFetch(path, { headers: { 'X-Avibe-Prefetch': '1' } });
  if (!response.ok) {
    throw Object.assign(new Error(`Prefetch failed: ${path} (${response.status})`), { status: response.status });
  }
  return response.json();
}

type NetworkInformationLike = { saveData?: boolean; metered?: boolean; type?: string };

/** The connection may carry a prefetch: no data saver, and not one the Network
 *  Information API reports as metered (cellular). Without the API there is
 *  nothing to go on, and the prefetch proceeds. */
function connectionAllowsPrefetch(): boolean {
  const nav = globalThis.navigator as (Navigator & { connection?: NetworkInformationLike }) | undefined;
  const connection = nav?.connection;
  if (!connection) return true;
  if (connection.saveData === true || connection.metered === true) return false;
  return connection.type !== 'cellular';
}

const pageVisible = () => typeof document === 'undefined' || document.visibilityState === 'visible';

function whenIdle(run: () => void): void {
  const idle = (globalThis as { requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number })
    .requestIdleCallback;
  if (typeof idle === 'function') idle(run, { timeout: IDLE_TIMEOUT_MS });
  else setTimeout(run, IDLE_FALLBACK_MS);
}

const errorStatus = (error: unknown): number | null => {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === 'number' ? status : null;
};

// Per document: a Session is tried once, whatever the outcome. A chat the reader
// opens stores itself; one that failed is not worth retrying in the background.
const tried = new Set<string>();
const queue: string[] = [];
let active = 0;
let idleScheduled = false;

/** Offer the Inbox's most recent Sessions, newest first. Those without a stored
 *  conversation are prefetched when the browser is idle. */
export function prefetchChatSnapshots(sessionIds: readonly string[]): void {
  if (!getLocalSnapshotPrincipal() || !pageVisible() || !connectionAllowsPrefetch()) return;
  for (const sessionId of sessionIds.slice(0, CHAT_PREFETCH_CANDIDATES)) {
    if (tried.has(sessionId)) continue;
    tried.add(sessionId);
    queue.push(sessionId);
  }
  schedule();
}

function schedule(): void {
  if (idleScheduled || active >= CHAT_PREFETCH_CONCURRENCY || queue.length === 0) return;
  idleScheduled = true;
  whenIdle(() => {
    idleScheduled = false;
    pump();
  });
}

function pump(): void {
  // Conditions may have changed since the Sessions were offered. What is left
  // goes back to untried, so the next offer under good conditions takes it.
  if (!getLocalSnapshotPrincipal() || !pageVisible() || !connectionAllowsPrefetch()) {
    for (const sessionId of queue.splice(0)) tried.delete(sessionId);
    return;
  }
  while (active < CHAT_PREFETCH_CONCURRENCY && queue.length > 0) {
    const sessionId = queue.shift()!;
    active += 1;
    void prefetchOne(sessionId).finally(() => {
      active -= 1;
      schedule();
    });
  }
}

async function prefetchOne(sessionId: string): Promise<void> {
  if (await readChatSnapshot(sessionId)) return;
  // Noted as the request starts, like the chat page's own: a reader change, a
  // store clear, or a forget of this Session while it is out voids the answer.
  const request = beginChatSnapshotRequest(sessionId);
  if (!request.principal) return;
  try {
    const bootstrap = await fetchBootstrapForPrefetch(sessionId);
    // The reader may have opened the chat meanwhile; its own record is newer.
    if (await readChatSnapshot(sessionId)) return;
    const owner = chatSnapshotOwnerForAnswer(request);
    if (owner) await writeChatSnapshot(owner, chatSnapshotFromBootstrap(bootstrap), { added: true });
  } catch (error) {
    const status = errorStatus(error);
    if (status === 403 || status === 404) await forgetChatSnapshot(sessionId);
  }
}
