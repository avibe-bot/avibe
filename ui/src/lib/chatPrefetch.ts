// Idle-time prefetch of the conversations a reader is most likely to open next
// (docs/plans/web-ui-local-cache.md section 4.2): the few most recent Inbox
// Sessions that have no stored snapshot get the same bootstrap the chat page
// reads, so the first tap on each paints at once too. The main Bot's
// conversation matters most, so it is always taken, first, and refreshed even
// when it is stored, unless it is the chat open now.
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
  chatSnapshotWriteCount,
  forgetChatSnapshot,
  readChatSnapshot,
  writeChatSnapshot,
} from './chatSnapshots';
import { getLocalSnapshotPrincipal } from './localSnapshots';
import { beginLocalStoreRead, forgetLocalStoreSession, recordLocalStorePage } from './localStore';

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

type NetworkInformationLike = Partial<EventTarget> & { saveData?: boolean; metered?: boolean; type?: string };

const networkInformation = (): NetworkInformationLike | undefined =>
  (globalThis.navigator as (Navigator & { connection?: NetworkInformationLike }) | undefined)?.connection;

/** The connection may carry a prefetch: no data saver, and not one the Network
 *  Information API reports as metered (cellular). Without the API there is
 *  nothing to go on, and the prefetch proceeds. */
function connectionAllowsPrefetch(): boolean {
  const connection = networkInformation();
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
// The pinned Session (the main Bot's) is refreshed once per document as well.
const tried = new Set<string>();
const refreshed = new Set<string>();
type Queued = { sessionId: string; refresh: boolean };
const queue: Queued[] = [];
let active = 0;
let idleScheduled = false;
// The latest Sessions offered. An offer made while the page was hidden or the
// connection metered, or given back before it ran, is offered again when the
// page shows or the connection changes, so a moment in the background or on
// cellular does not switch the prefetch off for the rest of the document.
let latestOffer: readonly string[] = [];
let latestPinned: string | null = null;
let latestOpen: string | null = null;
let watchingConditions = false;

const conditionsAllowPrefetch = () =>
  Boolean(getLocalSnapshotPrincipal()) && pageVisible() && connectionAllowsPrefetch();

function watchConditions(): void {
  if (watchingConditions) return;
  watchingConditions = true;
  const offerAgain = () => offer();
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', offerAgain);
  networkInformation()?.addEventListener?.('change', offerAgain);
}

/** Offer the Inbox's most recent Sessions, newest first. Those without a stored
 *  conversation are prefetched when the browser is idle. ``pinned`` (the main
 *  Bot's Session) goes first and is refreshed even when stored; ``open`` is the
 *  chat on screen, which keeps its own record current and is not taken. */
export function prefetchChatSnapshots(
  sessionIds: readonly string[],
  { pinned = null, open = null }: { pinned?: string | null; open?: string | null } = {},
): void {
  latestOffer = sessionIds.slice(0, CHAT_PREFETCH_CANDIDATES);
  latestPinned = pinned;
  latestOpen = open;
  watchConditions();
  offer();
}

function offer(): void {
  if (!conditionsAllowPrefetch()) return;
  const pinned = latestPinned;
  if (pinned && !refreshed.has(pinned)) {
    refreshed.add(pinned);
    tried.add(pinned);
    const queued = queue.findIndex((item) => item.sessionId === pinned);
    if (queued !== -1) queue.splice(queued, 1);
    queue.unshift({ sessionId: pinned, refresh: true });
  }
  for (const sessionId of latestOffer) {
    if (tried.has(sessionId)) continue;
    tried.add(sessionId);
    queue.push({ sessionId, refresh: false });
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
  // goes back to untried, and the next page show or connection change offers it
  // again.
  if (!conditionsAllowPrefetch()) {
    for (const { sessionId, refresh } of queue.splice(0)) {
      tried.delete(sessionId);
      if (refresh) refreshed.delete(sessionId);
    }
    return;
  }
  while (active < CHAT_PREFETCH_CONCURRENCY && queue.length > 0) {
    const item = queue.shift()!;
    // The chat on screen keeps its own record current.
    if (item.refresh && item.sessionId === latestOpen) continue;
    active += 1;
    void prefetchOne(item).finally(() => {
      active -= 1;
      schedule();
    });
  }
}

async function prefetchOne({ sessionId, refresh }: Queued): Promise<void> {
  if (!refresh && await readChatSnapshot(sessionId)) return;
  // Noted as the request starts, like the chat page's own: a reader change, a
  // store clear, or a forget of this Session while it is out voids the answer.
  const request = beginChatSnapshotRequest(sessionId);
  if (!request.principal) return;
  const writes = chatSnapshotWriteCount(sessionId);
  const stored = beginLocalStoreRead(sessionId);
  try {
    const bootstrap = await fetchBootstrapForPrefetch(sessionId);
    // The message store takes every bootstrap it is handed, whatever happens to
    // the snapshot below: P1 keeps a newer copy the open chat wrote.
    void recordLocalStorePage(stored, { kind: 'tail' }, bootstrap, bootstrap.session);
    // The reader may have opened the chat meanwhile; its own record is newer.
    // A refresh replaces a stored record, so it asks whether one was written
    // while it was out rather than whether one exists.
    if (refresh ? chatSnapshotWriteCount(sessionId) !== writes : await readChatSnapshot(sessionId)) return;
    const owner = chatSnapshotOwnerForAnswer(request);
    if (owner) await writeChatSnapshot(owner, chatSnapshotFromBootstrap(bootstrap), { added: true });
  } catch (error) {
    const status = errorStatus(error);
    if (status === 403 || status === 404) await Promise.all([forgetChatSnapshot(sessionId), forgetLocalStoreSession(sessionId)]);
  }
}
