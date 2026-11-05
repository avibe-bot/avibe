// The local message store: every transcript page and streamed row this client
// receives, kept in IndexedDB so conversations can open from the device and be
// read offline (docs/plans/local-sync-store.md, Phase 1). This module writes
// through what the client already reads; reading from it is the chat page's job.
//
// The rules that matter, each owned here:
//
//  * One reader at a time. The store belongs to the reader the snapshot store
//    names (lib/localSnapshots). A different reader, a sign-out, a revocation, an
//    authorization change, an expired session, or a newer sign-out generation
//    from any tab, of any build since #3228, empties it before anything else is
//    read or written.
//  * P1, last observed wins per row. Every row carries ``observedAt``: when the
//    request that returned it started, or when the event that carried it
//    arrived. A write replaces or deletes a row only when it is not older, and a
//    delete leaves a tombstone so an older answer still in flight cannot bring
//    the row back.
//  * P2, a page speaks only for what it proves. It replaces the rows inside its
//    reach: from its first row, or the start of history when nothing is older,
//    to its last row, or the open end when it is a tail. Coverage (./ranges) is
//    recorded from what it returned and joins other ranges only where they share
//    keys.
//  * Best effort. A missing, blocked, full, or corrupt database means the client
//    behaves as it did without this store; nothing here throws to a caller.

import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { isTranscriptMessage } from '../chatMessageTypes';
import {
  getLocalSnapshotEpoch,
  getLocalSnapshotPrincipal,
  registerLocalSnapshotClearHook,
  subscribeLocalSnapshotPrincipal,
} from '../localSnapshots';
import { isStandalonePwa } from '../platform';
import { readSignOutGeneration, SIGN_OUT_GENERATION_KEY } from '../signOutGeneration';
import {
  type CoverageRange,
  type OrderKey,
  type PageReach,
  compareKeys,
  extendTailCoverage,
  isCovered,
  mergePageCoverage,
  orderKeyOf,
} from './ranges';

const DB_NAME = 'avibe-local-store';
const DB_VERSION = 1;
const META = 'meta';
const SESSIONS = 'sessions';
const MESSAGES = 'messages';
const RANGES = 'ranges';
const TOMBSTONES = 'tombstones';
const ALL_STORES = [META, SESSIONS, MESSAGES, RANGES, TOMBSTONES] as const;
const BY_SESSION = 'bySession';
const BY_OBSERVED = 'byObserved';

/** Bumped by hand when a stored shape changes; a store written under another
 *  schema is emptied when it is opened. */
const STORE_SCHEMA = 1;
/** An answer whose request started longer ago than this is dropped, which
 *  bounds how long a tombstone has to outlive the row it removed. */
export const LOCAL_STORE_MAX_ANSWER_AGE_MS = 2 * 60 * 1000;
const TOMBSTONE_TTL_MS = 10 * 60 * 1000;
/** Decision D2: 1 GB, or half the origin quota when that is smaller. */
export const LOCAL_STORE_BUDGET_BYTES = 1024 * 1024 * 1024;
/** Eviction stops this far under the budget so it does not run on every write. */
const EVICTION_TARGET = 0.9;
const BUDGET_CHECK_INTERVAL_MS = 30 * 1000;
/** Sessions kept whatever the budget: the most recently active ones. */
const PROTECTED_RECENT_SESSIONS = 40;
const MAIN_BOT_SESSION_ID = 'ses_main';

type Meta = { key: 'reader'; reader: string; schema: number; signOutGeneration: number; persisted: boolean | null };
type StoredSession = {
  id: string;
  /** Null while only rows of the session have arrived, before any session read. */
  row: WorkbenchSession | null;
  observedAt: number;
  touchedAt: number;
  lastActiveAt: string;
  bytes: number;
};
type StoredMessage = {
  id: string;
  sessionId: string;
  orderAt: string;
  row: WorkbenchMessage;
  observedAt: number;
  bytes: number;
};
type StoredRanges = { sessionId: string; ranges: CoverageRange[] };
type Tombstone = { id: string; sessionId: string; observedAt: number };

// ---------------------------------------------------------------------------
// Database

let dbPromise: Promise<IDBDatabase | null> | null = null;

function idbFactory(): IDBFactory | null {
  try {
    return typeof indexedDB === 'undefined' ? null : indexedDB;
  } catch {
    return null;
  }
}

function openDb(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise;
  const factory = idbFactory();
  if (!factory) return Promise.resolve(null);
  dbPromise = new Promise<IDBDatabase | null>((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = factory.open(DB_NAME, DB_VERSION);
    } catch {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(META)) db.createObjectStore(META, { keyPath: 'key' });
      if (!db.objectStoreNames.contains(SESSIONS)) db.createObjectStore(SESSIONS, { keyPath: 'id' });
      if (!db.objectStoreNames.contains(MESSAGES)) {
        db.createObjectStore(MESSAGES, { keyPath: 'id' }).createIndex(BY_SESSION, ['sessionId', 'orderAt', 'id']);
      }
      if (!db.objectStoreNames.contains(RANGES)) db.createObjectStore(RANGES, { keyPath: 'sessionId' });
      if (!db.objectStoreNames.contains(TOMBSTONES)) {
        const tombstones = db.createObjectStore(TOMBSTONES, { keyPath: 'id' });
        tombstones.createIndex(BY_SESSION, 'sessionId');
        tombstones.createIndex(BY_OBSERVED, 'observedAt');
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => {
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
  return dbPromise;
}

function settle<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

function isQuotaError(error: unknown): boolean {
  return error instanceof DOMException
    && (error.name === 'QuotaExceededError' || error.name === 'NS_ERROR_DOM_QUOTA_REACHED');
}

/** Run ``work`` in one transaction over ``stores``. Null when the database is
 *  unavailable or the work failed; a quota failure evicts once and retries. */
async function transact<T>(
  mode: IDBTransactionMode,
  stores: readonly string[],
  work: (tx: IDBTransaction) => Promise<T> | T,
  { retryOnQuota = true }: { retryOnQuota?: boolean } = {},
): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  try {
    const tx = db.transaction(stores as string[], mode);
    const result = await work(tx);
    await done(tx);
    return result;
  } catch (error) {
    if (!isQuotaError(error)) return null;
    if (retryOnQuota && (await evictForSpace(true))) return transact(mode, stores, work, { retryOnQuota: false });
    await wipe();
    return null;
  }
}

const byteSize = (value: unknown): number => {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).length;
  } catch {
    return 0;
  }
};

// ---------------------------------------------------------------------------
// The reader, and every fence that voids a write

// Advanced by every wipe in this document. A write captures it when its request
// starts and is dropped if a wipe happened since.
let wipeEpoch = 0;
// Advanced per session by every forget, with the same purpose.
const forgotten = new Map<string, number>();
// Names the event stream rows are arriving on. A reconnect, a resumed page, or
// a recovered controller leg starts a new one: whatever the old one would have
// delivered may be missing, so no range keeps a tail claim across it.
const documentId = Math.random().toString(36).slice(2);
let streamEpoch = 0;
const currentStreamToken = () => `${documentId}:${streamEpoch}`;

// Sessions this document started a page read for, and when. A row streamed
// while such a read is out is kept even though the session has no stored range
// yet: the page lands after it, and its tail coverage reaches over it.
const pendingReads = new Map<string, number>();

// The reader whose meta record is in place, so writes may proceed.
let readyReader: { reader: string; epoch: number; promise: Promise<boolean> } | null = null;

/** Open the store for ``reader``: empty it first when it was written for
 *  someone else, under another schema, or before a newer sign-out anywhere. */
function ensureReader(reader: string): Promise<boolean> {
  if (readyReader && readyReader.reader === reader && readyReader.epoch === wipeEpoch) return readyReader.promise;
  const epoch = wipeEpoch;
  const promise = (async () => {
    const generation = readSignOutGeneration();
    const opened = await transact('readwrite', ALL_STORES, async (tx) => {
      const meta = await settle(tx.objectStore(META).get('reader') as IDBRequest<Meta | undefined>);
      if (!meta || meta.reader !== reader || meta.schema !== STORE_SCHEMA || meta.signOutGeneration < generation) {
        for (const store of ALL_STORES) tx.objectStore(store).clear();
        const fresh: Meta = { key: 'reader', reader, schema: STORE_SCHEMA, signOutGeneration: generation, persisted: null };
        tx.objectStore(META).put(fresh);
      }
      return true;
    });
    return opened === true && epoch === wipeEpoch && getLocalSnapshotPrincipal() === reader;
  })();
  readyReader = { reader, epoch, promise };
  void promise.then((ok) => {
    if (ok) void requestPersistence();
  });
  return promise;
}

async function wipe(): Promise<void> {
  wipeEpoch += 1;
  readyReader = null;
  forgotten.clear();
  pendingReads.clear();
  await transact('readwrite', ALL_STORES, (tx) => {
    for (const store of ALL_STORES) tx.objectStore(store).clear();
  }, { retryOnQuota: false });
}

/** Empty the store. Sign-out and revocation reach here through the snapshot
 *  store's clear (``registerLocalSnapshotClearHook``); an expired session calls
 *  it directly. */
export function wipeLocalStore(): Promise<void> {
  return wipe();
}

// A new reader, or none, voids what the store held for the last one. Whether it
// is emptied is decided when the new reader opens it (``ensureReader``), so a
// provisional replay of the same reader keeps its data.
subscribeLocalSnapshotPrincipal(() => {
  readyReader = null;
});
// Every whole-store clear of the snapshots (sign-out, revocation, an
// authorization change, a quota failure) is a clear of this store too, awaited
// by whoever cleared, so sign-out does not navigate away before it lands.
registerLocalSnapshotClearHook(wipe);
// A sign-out in another tab, including one running an older build that does not
// know this store exists, bumps the shared generation. Empty the store now
// rather than at the next open.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key === SIGN_OUT_GENERATION_KEY) void wipe();
  });
}

/** What the store looked like when a read started. Its answer may be written
 *  only while all of it still holds. */
export type LocalStoreTicket = {
  readonly sessionId: string;
  readonly reader: string | null;
  readonly snapshotEpoch: number;
  readonly wipeEpoch: number;
  readonly forgotten: number;
  readonly observedAt: number;
  readonly streamToken: string;
};

/** Note the store's state as a read of ``sessionId`` starts. ``observedAt`` is
 *  when the request went out; a cached answer passes its original start. */
export function beginLocalStoreRead(sessionId: string, observedAt = Date.now()): LocalStoreTicket {
  pendingReads.set(sessionId, Math.max(pendingReads.get(sessionId) ?? 0, observedAt));
  return ticketFor(sessionId, observedAt);
}

function ticketFor(sessionId: string, observedAt: number): LocalStoreTicket {
  return {
    sessionId,
    reader: getLocalSnapshotPrincipal(),
    snapshotEpoch: getLocalSnapshotEpoch(),
    wipeEpoch,
    forgotten: forgotten.get(sessionId) ?? 0,
    observedAt,
    streamToken: currentStreamToken(),
  };
}

const readPending = (sessionId: string) =>
  Date.now() - (pendingReads.get(sessionId) ?? -Infinity) <= LOCAL_STORE_MAX_ANSWER_AGE_MS;

/** The reader a ticket's answer belongs to, or null when it may not be stored.
 *  A read that started before any reader was named belongs to the one named
 *  since, unless a clear, wipe, or forget happened meanwhile. */
function ticketReader(ticket: LocalStoreTicket): string | null {
  const reader = ticket.reader ?? getLocalSnapshotPrincipal();
  if (!reader || reader !== getLocalSnapshotPrincipal()) return null;
  if (ticket.snapshotEpoch !== getLocalSnapshotEpoch() || ticket.wipeEpoch !== wipeEpoch) return null;
  if (ticket.forgotten !== (forgotten.get(ticket.sessionId) ?? 0)) return null;
  if (Date.now() - ticket.observedAt > LOCAL_STORE_MAX_ANSWER_AGE_MS) return null;
  return reader;
}

/** Ready to write for this ticket: the reader is open and nothing voided it. */
async function writableFor(ticket: LocalStoreTicket): Promise<boolean> {
  const reader = ticketReader(ticket);
  if (!reader || !(await ensureReader(reader))) return false;
  return ticketReader(ticket) === reader;
}

// ---------------------------------------------------------------------------
// Rows

/** A row the server stored: a transcript Message, not a Delivery still being
 *  sent and not the projection of a claimed input. */
export const isStoredTranscriptRow = (row: WorkbenchMessage): boolean =>
  isTranscriptMessage(row)
  && !row.projection
  && row.state == null
  && (row as { delivery_id?: unknown }).delivery_id == null;

const isRow = (value: unknown): value is WorkbenchMessage =>
  typeof value === 'object' && value !== null
  && typeof (value as { id?: unknown }).id === 'string'
  && typeof (value as { type?: unknown }).type === 'string';

type RowWrite = { tx: IDBTransaction; sessionDelta: Map<string, number> };

/** Put ``row`` under P1: skipped when the store holds a newer copy or a newer
 *  tombstone for it. */
async function putRow({ tx, sessionDelta }: RowWrite, row: WorkbenchMessage, sessionId: string, observedAt: number): Promise<boolean> {
  const messages = tx.objectStore(MESSAGES);
  const tombstones = tx.objectStore(TOMBSTONES);
  const [existing, tombstone] = await Promise.all([
    settle(messages.get(row.id) as IDBRequest<StoredMessage | undefined>),
    settle(tombstones.get(row.id) as IDBRequest<Tombstone | undefined>),
  ]);
  if ((existing && existing.observedAt > observedAt) || (tombstone && tombstone.observedAt > observedAt)) return false;
  const stored: StoredMessage = {
    id: row.id,
    sessionId,
    orderAt: orderKeyOf(row).at,
    row,
    observedAt,
    bytes: byteSize(row),
  };
  messages.put(stored);
  if (tombstone) tombstones.delete(row.id);
  if (existing) sessionDelta.set(existing.sessionId, (sessionDelta.get(existing.sessionId) ?? 0) - existing.bytes);
  sessionDelta.set(sessionId, (sessionDelta.get(sessionId) ?? 0) + stored.bytes);
  return true;
}

function sessionKeyRange(sessionId: string, low: OrderKey | null, high: OrderKey | null): IDBKeyRange {
  // Arrays sort after strings in IndexedDB key order, so ``[sessionId]`` is below
  // and ``[sessionId, []]`` above every ``[sessionId, orderAt, id]``.
  const lower = low ? [sessionId, low.at, low.id] : [sessionId];
  const upper = high ? [sessionId, high.at, high.id] : [sessionId, []];
  return IDBKeyRange.bound(lower, upper);
}

/** Keep each session's byte count in step with its rows, so eviction sees
 *  every row, including rows of a session no session read has named yet. */
async function applySessionDelta(tx: IDBTransaction, sessionDelta: Map<string, number>, touchedAt: number): Promise<void> {
  const sessions = tx.objectStore(SESSIONS);
  for (const [sessionId, delta] of sessionDelta) {
    const stored = await settle(sessions.get(sessionId) as IDBRequest<StoredSession | undefined>)
      ?? { id: sessionId, row: null, observedAt: 0, touchedAt, lastActiveAt: '', bytes: 0 };
    sessions.put({ ...stored, bytes: Math.max(0, stored.bytes + delta), touchedAt: Math.max(stored.touchedAt, touchedAt) } satisfies StoredSession);
  }
}

// ---------------------------------------------------------------------------
// Pages

export type LocalStorePageRequest = { kind: 'tail' } | { kind: 'before' } | { kind: 'around' };

export type LocalStorePageAnswer = {
  messages?: WorkbenchMessage[] | null;
  next_before_id?: string | null;
  next_after_id?: string | null;
  anchor_id?: string | null;
};

/** What ``answer`` proves, or null when it proves nothing it may be applied
 *  for: an ``around`` window whose anchor the server could not resolve, or an
 *  empty page that does not reach both ends. */
function reachOf(request: LocalStorePageRequest, answer: LocalStorePageAnswer, rows: WorkbenchMessage[]): PageReach | null {
  if (request.kind === 'around' && !answer.anchor_id) return null;
  const keys = rows.map(orderKeyOf).sort(compareKeys);
  const first = keys[0] ?? null;
  const last = keys[keys.length - 1] ?? null;
  const startReached = answer.next_before_id == null;
  const tail = request.kind === 'tail' || (request.kind === 'around' && answer.next_after_id == null);
  if (!first && !(startReached && tail)) return null;
  return { low: startReached ? null : first, high: tail ? null : last, first, last };
}

/** Write a server page for ``ticket``'s session under P1 and P2. */
export async function recordLocalStorePage(
  ticket: LocalStoreTicket,
  request: LocalStorePageRequest,
  answer: LocalStorePageAnswer,
  session?: WorkbenchSession | null,
): Promise<void> {
  if (session?.status === 'archived') {
    await forgetLocalStoreSession(ticket.sessionId);
    return;
  }
  // Only rows the server stored decide the reach. A claimed-input projection is
  // appended to a tail whatever its age, so its key says nothing about which
  // stretch of history the page covers.
  const stored = (Array.isArray(answer.messages) ? answer.messages.filter(isRow) : [])
    .filter((row) => row.session_id === ticket.sessionId && isStoredTranscriptRow(row));
  const reach = reachOf(request, answer, stored);
  if (!reach && !session) return;
  if (!(await writableFor(ticket))) return;
  const { sessionId, observedAt } = ticket;
  const now = Date.now();
  await transact('readwrite', [SESSIONS, MESSAGES, RANGES, TOMBSTONES], async (tx) => {
    if (ticketReader(ticket) === null) return;
    if (session && session.id === sessionId) await putSession(tx, session, observedAt, now);
    if (!reach) return;
    const write: RowWrite = { tx, sessionDelta: new Map() };
    const returned = new Set(stored.map((row) => row.id));
    const messages = tx.objectStore(MESSAGES);
    const inReach = await settle(
      messages.index(BY_SESSION).getAll(sessionKeyRange(sessionId, reach.low, reach.high)) as IDBRequest<StoredMessage[]>,
    );
    // Rows newer than the page (P1) survive it. Those above a tail's last row
    // arrived on the stream while it was out; when that stream stayed continuous
    // since the request started, the tail's coverage reaches over them.
    let streamedAbove: OrderKey | null = null;
    for (const row of inReach) {
      if (returned.has(row.id)) continue;
      if (row.observedAt > observedAt) {
        const key = orderKeyOf(row.row);
        if (reach.high === null && (!reach.last || compareKeys(key, reach.last) > 0)
          && (!streamedAbove || compareKeys(key, streamedAbove) > 0)) streamedAbove = key;
        continue;
      }
      messages.delete(row.id);
      tx.objectStore(TOMBSTONES).put({ id: row.id, sessionId, observedAt } satisfies Tombstone);
      write.sessionDelta.set(sessionId, (write.sessionDelta.get(sessionId) ?? 0) - row.bytes);
    }
    for (const row of stored) await putRow(write, row, sessionId, observedAt);
    const rangesStore = tx.objectStore(RANGES);
    const current = await settle(rangesStore.get(sessionId) as IDBRequest<StoredRanges | undefined>);
    const streamToken = currentStreamToken();
    let ranges = mergePageCoverage(current?.ranges ?? [], reach, { streamToken, now });
    if (streamedAbove && ticket.streamToken === streamToken) ranges = extendTailCoverage(ranges, streamedAbove, streamToken);
    rangesStore.put({ sessionId, ranges } satisfies StoredRanges);
    await applySessionDelta(tx, write.sessionDelta, now);
  });
  scheduleBudgetCheck();
}

async function putSession(tx: IDBTransaction, session: WorkbenchSession, observedAt: number, now: number): Promise<void> {
  const sessions = tx.objectStore(SESSIONS);
  const existing = await settle(sessions.get(session.id) as IDBRequest<StoredSession | undefined>);
  if (existing && existing.observedAt > observedAt) return;
  sessions.put({
    id: session.id,
    row: session,
    observedAt,
    touchedAt: Math.max(existing?.touchedAt ?? 0, now),
    lastActiveAt: session.last_active_at || session.updated_at || '',
    bytes: existing?.bytes ?? 0,
  } satisfies StoredSession);
}

/** Store a session row the client received on its own (a session read). */
export async function recordLocalStoreSession(ticket: LocalStoreTicket, session: WorkbenchSession | null | undefined): Promise<void> {
  if (!session || session.id !== ticket.sessionId) return;
  if (session.status === 'archived') {
    await forgetLocalStoreSession(session.id);
    return;
  }
  if (!(await writableFor(ticket))) return;
  const now = Date.now();
  await transact('readwrite', [SESSIONS], async (tx) => {
    if (ticketReader(ticket) === null) return;
    await putSession(tx, session, ticket.observedAt, now);
  });
}

// ---------------------------------------------------------------------------
// The event stream

/** A row from ``message.new`` or ``message.updated``. Stored only for sessions
 *  the store already holds or this document is reading; it extends the tail
 *  range when the stream that carried it has been continuous since that range
 *  was verified. */
export async function recordLocalStoreStreamRow(row: WorkbenchMessage | null | undefined): Promise<void> {
  if (!row || !isRow(row) || !row.session_id || !isStoredTranscriptRow(row)) return;
  const sessionId = row.session_id;
  // Not a read: an arriving row does not make its own session one worth keeping.
  const ticket = ticketFor(sessionId, Date.now());
  const streamToken = currentStreamToken();
  if (!(await writableFor(ticket))) return;
  const now = Date.now();
  await transact('readwrite', [SESSIONS, MESSAGES, RANGES, TOMBSTONES], async (tx) => {
    if (ticketReader(ticket) === null) return;
    const rangesStore = tx.objectStore(RANGES);
    const current = await settle(rangesStore.get(sessionId) as IDBRequest<StoredRanges | undefined>);
    if (!current && !readPending(sessionId)) return;
    const write: RowWrite = { tx, sessionDelta: new Map() };
    if (!(await putRow(write, row, sessionId, ticket.observedAt))) return;
    if (current && streamToken === currentStreamToken()) {
      rangesStore.put({ sessionId, ranges: extendTailCoverage(current.ranges, orderKeyOf(row), streamToken) } satisfies StoredRanges);
    }
    await applySessionDelta(tx, write.sessionDelta, now);
  });
  scheduleBudgetCheck();
}

/** The event stream may have missed something: a new stream connected, a page
 *  resumed without proof its stream survived, or the controller leg recovered.
 *  No range keeps a tail claim across it. */
export function noteLocalStoreStreamGap(): void {
  streamEpoch += 1;
}

// ---------------------------------------------------------------------------
// Forgetting one session

/** Delete everything stored for ``sessionId`` and void every read of it still
 *  in flight: it was archived, left the reader's Inbox, or the server refused it. */
export async function forgetLocalStoreSession(sessionId: string): Promise<void> {
  forgotten.set(sessionId, (forgotten.get(sessionId) ?? 0) + 1);
  await transact('readwrite', [SESSIONS, MESSAGES, RANGES, TOMBSTONES], async (tx) => {
    const messages = tx.objectStore(MESSAGES);
    const keys = await settle(messages.index(BY_SESSION).getAllKeys(sessionKeyRange(sessionId, null, null)));
    for (const key of keys) messages.delete(key);
    const tombstones = tx.objectStore(TOMBSTONES);
    const tombstoneKeys = await settle(tombstones.index(BY_SESSION).getAllKeys(IDBKeyRange.only(sessionId)));
    for (const key of tombstoneKeys) tombstones.delete(key);
    tx.objectStore(RANGES).delete(sessionId);
    tx.objectStore(SESSIONS).delete(sessionId);
  }, { retryOnQuota: false });
}

/** A ``session.activity`` event that takes the Session out of the reader's
 *  Inbox (archived, or moved to the background) deletes it from the store. */
export function forgetLocalStoreSessionOnActivity(data: { session_id?: string | null; event?: string; visibility?: string }): void {
  if (!data.session_id) return;
  if (data.event === 'archived' || data.visibility === 'background') void forgetLocalStoreSession(data.session_id);
}

// ---------------------------------------------------------------------------
// Budget and persistence

let persistenceRequested = false;

/** Decision D5: ask once per document, silently, where the browser decides
 *  without a prompt and the app is installed. A plain browser tab does not ask:
 *  Firefox would show a prompt there. */
async function requestPersistence(): Promise<void> {
  if (persistenceRequested || !isStandalonePwa()) return;
  persistenceRequested = true;
  const storage = typeof navigator === 'undefined' ? undefined : navigator.storage;
  if (!storage?.persist) return;
  let persisted: boolean | null = null;
  try {
    persisted = (await storage.persisted?.()) || (await storage.persist());
  } catch {
    persisted = null;
  }
  await transact('readwrite', [META], async (tx) => {
    const meta = await settle(tx.objectStore(META).get('reader') as IDBRequest<Meta | undefined>);
    if (meta) tx.objectStore(META).put({ ...meta, persisted } satisfies Meta);
  }, { retryOnQuota: false });
}

/** The bytes the store may use: decision D2's budget, or half the origin quota
 *  when that is smaller. */
export async function localStoreBudget(): Promise<number> {
  try {
    const estimate = await navigator.storage?.estimate?.();
    if (estimate?.quota && estimate.quota > 0) return Math.min(LOCAL_STORE_BUDGET_BYTES, estimate.quota / 2);
  } catch {
    // Fall back to the budget alone; a quota error still evicts.
  }
  return LOCAL_STORE_BUDGET_BYTES;
}

let budgetCheckTimer: ReturnType<typeof setTimeout> | null = null;
let lastBudgetCheck = 0;

function scheduleBudgetCheck(): void {
  if (budgetCheckTimer) return;
  const wait = Math.max(0, lastBudgetCheck + BUDGET_CHECK_INTERVAL_MS - Date.now());
  budgetCheckTimer = setTimeout(() => {
    budgetCheckTimer = null;
    lastBudgetCheck = Date.now();
    void evictForSpace(false);
  }, wait);
}

/** Evict whole sessions, least recently active first, until the store is under
 *  its budget (or, after a quota failure, until one session is gone). The main
 *  Bot's session, pinned sessions, and the most recently active ones go last.
 *  Also purges tombstones no answer can still be raced by. Returns whether it
 *  freed anything. */
async function evictForSpace(quotaFailure: boolean): Promise<boolean> {
  const budget = quotaFailure ? 0 : await localStoreBudget();
  const db = await openDb();
  if (!db) return false;
  let freed = false;
  try {
    const tx = db.transaction([SESSIONS, MESSAGES, RANGES, TOMBSTONES], 'readwrite');
    const tombstones = tx.objectStore(TOMBSTONES);
    const expired = await settle(
      tombstones.index(BY_OBSERVED).getAllKeys(IDBKeyRange.upperBound(Date.now() - TOMBSTONE_TTL_MS)),
    );
    for (const key of expired) tombstones.delete(key);
    const sessions = await settle(tx.objectStore(SESSIONS).getAll() as IDBRequest<StoredSession[]>);
    let total = sessions.reduce((sum, session) => sum + session.bytes, 0);
    if (quotaFailure || total > budget) {
      const target = budget * EVICTION_TARGET;
      const byRecency = [...sessions].sort((a, b) => recency(b).localeCompare(recency(a)));
      const recent = new Set(byRecency.slice(0, PROTECTED_RECENT_SESSIONS).map((session) => session.id));
      const evictable = byRecency
        .filter((session) => session.bytes > 0)
        .sort((a, b) => evictionRank(a, recent) - evictionRank(b, recent) || recency(a).localeCompare(recency(b)));
      for (const session of evictable) {
        if (!quotaFailure && total <= target) break;
        const keys = await settle(
          tx.objectStore(MESSAGES).index(BY_SESSION).getAllKeys(sessionKeyRange(session.id, null, null)),
        );
        for (const key of keys) tx.objectStore(MESSAGES).delete(key);
        tx.objectStore(RANGES).delete(session.id);
        tx.objectStore(SESSIONS).put({ ...session, bytes: 0 } satisfies StoredSession);
        total -= session.bytes;
        freed = true;
        if (quotaFailure) break;
      }
    }
    await done(tx);
  } catch {
    return false;
  }
  return freed;
}

/** How recently the conversation was active, by the server's clock. A session
 *  row without one falls back to when this device last wrote it. */
const recency = (session: StoredSession) => session.lastActiveAt || new Date(session.touchedAt).toISOString();

/** Lower ranks go first. */
function evictionRank(session: StoredSession, recent: Set<string>): number {
  if (session.id === MAIN_BOT_SESSION_ID) return 3;
  if (session.row?.pinned) return 2;
  if (recent.has(session.id)) return 1;
  return 0;
}

// ---------------------------------------------------------------------------
// Reading (the chat page's local-first read path, docs/plans/local-sync-store.md
// section 4.3, is its first caller)

export type LocalStoreWindow = {
  session: WorkbenchSession | null;
  /** Transcript rows in server order, newest last. */
  messages: WorkbenchMessage[];
  /** Whether every row from the oldest returned one to the newest is on the
   *  device, and whether nothing older exists. */
  contiguous: boolean;
  startReached: boolean;
};

/** Up to ``limit`` of the session's newest stored rows, or those just older than
 *  ``before``, with what the store can vouch for about them. */
export async function readLocalStoreWindow(
  sessionId: string,
  { before = null, limit = 50 }: { before?: OrderKey | null; limit?: number } = {},
): Promise<LocalStoreWindow | null> {
  const reader = getLocalSnapshotPrincipal();
  if (!reader || !(await ensureReader(reader))) return null;
  const epoch = wipeEpoch;
  const result = await transact('readonly', [SESSIONS, MESSAGES, RANGES], async (tx) => {
    const session = await settle(tx.objectStore(SESSIONS).get(sessionId) as IDBRequest<StoredSession | undefined>);
    const ranges = (await settle(tx.objectStore(RANGES).get(sessionId) as IDBRequest<StoredRanges | undefined>))?.ranges ?? [];
    const rows: StoredMessage[] = [];
    const upper = before ? IDBKeyRange.bound([sessionId], [sessionId, before.at, before.id], false, true) : sessionKeyRange(sessionId, null, null);
    await new Promise<void>((resolve, reject) => {
      const cursor = tx.objectStore(MESSAGES).index(BY_SESSION).openCursor(upper, 'prev');
      cursor.onsuccess = () => {
        const current = cursor.result;
        if (!current || rows.length >= limit) {
          resolve();
          return;
        }
        rows.push(current.value as StoredMessage);
        current.continue();
      };
      cursor.onerror = () => reject(cursor.error);
    });
    rows.reverse();
    const token = currentStreamToken();
    const contiguous = rows.length > 0
      && ranges.some((range) => isCovered([range], orderKeyOf(rows[0].row), token) && isCovered([range], orderKeyOf(rows[rows.length - 1].row), token));
    const startReached = rows.length > 0 && ranges.some((range) => range.from === null && isCovered([range], orderKeyOf(rows[0].row), token));
    return {
      session: session?.row ?? null,
      messages: rows.map((row) => row.row),
      contiguous,
      startReached,
    } satisfies LocalStoreWindow;
  });
  if (!result || epoch !== wipeEpoch || getLocalSnapshotPrincipal() !== reader) return null;
  return result;
}
