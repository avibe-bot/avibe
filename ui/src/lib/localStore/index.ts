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
  withinReach,
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

type Meta = {
  key: 'reader';
  /** Null while the store belongs to no one: after a sign-out, a revocation, or an expiry. */
  reader: string | null;
  schema: number;
  /** Advanced by every wipe in any tab; mirrored in localStorage (``STORE_EPOCH_KEY``). */
  epoch: number;
  signOutGeneration: number;
  persisted: boolean | null;
};
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
/** A page that landed: what it proved, and when it was read. A row a newer
 *  page's reach left out stays out, even when no copy of it was stored to
 *  tombstone. Kept only as long as an older answer could still land. */
type Watermark = { low: OrderKey | null; high: OrderKey | null; observedAt: number };
/** ``forgottenAt``: the session was forgotten then (in any tab). It stays on
 *  the record, through later writes, until no answer read before it can still
 *  land; ``ranges`` hold only what was written since. An answer read
 *  before it may not bring it back; the marker outlives every such answer. */
type StoredRanges = { sessionId: string; ranges: CoverageRange[]; watermarks?: Watermark[]; forgottenAt?: number };
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
    // A failed request's error event reaches the transaction before it aborts
    // and before ``tx.error`` is set, so keep the first one for the abort to
    // report: a quota failure must still read as one.
    let requestError: DOMException | null = null;
    tx.oncomplete = () => resolve();
    tx.onerror = (event) => {
      requestError ??= (event.target as IDBRequest | null)?.error ?? null;
    };
    tx.onabort = () => reject(tx.error ?? requestError);
  });
}

function isQuotaError(error: unknown): boolean {
  return error instanceof DOMException
    && (error.name === 'QuotaExceededError' || error.name === 'NS_ERROR_DOM_QUOTA_REACHED');
}

/** What a transaction does when it runs out of space: evict one session and
 *  retry once, then wipe (``evict``); wipe at once (``wipe``); or give up
 *  (``none``), which the wipe itself does, since wiping again from there could
 *  never end. */
type QuotaRecovery = 'evict' | 'wipe' | 'none';

/** Run ``work`` in one transaction over ``stores``. Null when the database is
 *  unavailable or the work failed. */
async function transact<T>(
  mode: IDBTransactionMode,
  stores: readonly string[],
  work: (tx: IDBTransaction) => Promise<T> | T,
  { onQuota = 'evict' }: { onQuota?: QuotaRecovery } = {},
): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  try {
    const tx = db.transaction(stores as string[], mode);
    const result = await work(tx);
    await done(tx);
    return result;
  } catch (error) {
    if (!isQuotaError(error) || onQuota === 'none') return null;
    if (onQuota === 'evict' && (await evictForSpace(true))) return transact(mode, stores, work, { onQuota: 'wipe' });
    await wipe(keepableReader());
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
//
// The database is shared by every tab of the origin, so the fences that decide
// whether a write may land live in it too. ``meta`` names the reader the store
// belongs to and its epoch: a counter every wipe in any tab advances. The same
// counter is mirrored in localStorage, which a read can note synchronously when
// it starts; its answer is written only if ``meta`` still names the same reader
// at the same epoch, checked inside the write's own transaction. The in-memory
// fences below only drop doomed writes early.

const STORE_EPOCH_KEY = 'avibe.local-store.epoch.v1';

function readStoreEpoch(): number {
  try {
    const raw = window.localStorage.getItem(STORE_EPOCH_KEY);
    const value = raw === null ? 0 : Number(raw);
    return Number.isFinite(value) ? value : 0;
  } catch {
    return 0;
  }
}

function writeStoreEpoch(epoch: number): void {
  try {
    window.localStorage.setItem(STORE_EPOCH_KEY, String(epoch));
  } catch {
    // Without localStorage no read can note the epoch, so writes are refused
    // (``writerFor``): the store stays empty rather than unfenced.
  }
}

/** A new epoch above both the mirror and ``meta``'s own: the mirror may have
 *  been cleared on its own, and an epoch must never repeat. */
function nextStoreEpoch(meta: Meta | undefined): number {
  const next = Math.max(readStoreEpoch(), meta?.epoch ?? 0) + 1;
  writeStoreEpoch(next);
  return next;
}

// Set, synchronously, when a wipe starts and removed once it has committed. A
// wipe the page did not live to finish (a navigation, a closed tab) is
// therefore finished by the next claim, in any document, before anything is
// read or written.
const WIPE_PENDING_KEY = 'avibe.local-store.wipe-pending.v1';

function markWipePending(): string {
  const token = `${Date.now()}:${Math.random().toString(36).slice(2)}`;
  try {
    window.localStorage.setItem(WIPE_PENDING_KEY, token);
  } catch {
    // Without localStorage the store takes no writes anyway (``writeStoreEpoch``).
  }
  return token;
}

function wipePending(): boolean {
  try {
    return window.localStorage.getItem(WIPE_PENDING_KEY) !== null;
  } catch {
    return false;
  }
}

function settleWipePending(token: string | null): void {
  try {
    if (token === null || window.localStorage.getItem(WIPE_PENDING_KEY) === token) window.localStorage.removeItem(WIPE_PENDING_KEY);
  } catch {
    // Nothing more to do.
  }
}

const getMeta = (tx: IDBTransaction) => settle(tx.objectStore(META).get('reader') as IDBRequest<Meta | undefined>);

function putMeta(tx: IDBTransaction, fields: Omit<Meta, 'key' | 'schema'>): void {
  tx.objectStore(META).put({ key: 'reader', schema: STORE_SCHEMA, ...fields } satisfies Meta);
}

/** Whether, inside ``tx``, the store still belongs to ``reader`` at ``epoch``. */
async function metaAdmits(tx: IDBTransaction, reader: string, epoch: number): Promise<boolean> {
  const meta = await getMeta(tx);
  return !!meta && meta.reader === reader && meta.epoch === epoch && meta.schema === STORE_SCHEMA
    && meta.signOutGeneration === readSignOutGeneration();
}

// Names the event stream rows are arriving on. A reconnect, a resumed page, or
// a recovered controller leg starts a new one: whatever the old one would have
// delivered may be missing, so no range keeps a tail claim across it.
const documentId = Math.random().toString(36).slice(2);
let streamEpoch = 0;
const currentStreamToken = () => `${documentId}:${streamEpoch}`;
// Whether the event stream is connected now. While it is not, nothing would
// deliver a newer row, so no page earns a tail claim and none is extended.
let streamLive = false;

// Sessions this document started a page read for, and when. A row streamed
// while such a read is out is kept even though the session has no stored range
// yet: the page lands after it, and its tail coverage reaches over it.
const pendingReads = new Map<string, number>();

// The store is claimed for a reader only when this document names one: the
// snapshot store's principal, which the server confirmed or the boot replayed.
// Nothing else takes the store over, so a tab still naming an old reader cannot
// empty and claim a store another tab claimed for the reader signed in now.
/** A claim that landed. When it had to empty the store it moved the epoch from
 *  ``from`` to ``epoch``; a read this document started just before, for the
 *  same reader, belongs to the claimed store rather than to the emptied one. */
type Claimed = { from: number; epoch: number };
let claim: { reader: string; promise: Promise<Claimed | null> } | null = null;

/** Claim the store for ``reader``: empty it first when it belongs to no one or
 *  someone else, was written under another schema, or predates a newer sign-out. */
function claimReader(reader: string): Promise<Claimed | null> {
  const promise = transact('readwrite', ALL_STORES, async (tx): Promise<Claimed> => {
    const generation = readSignOutGeneration();
    const meta = await getMeta(tx);
    // What reads this document started before the claim noted.
    const from = readStoreEpoch();
    const unfinishedWipe = wipePending();
    if (!unfinishedWipe && meta && meta.reader === reader && meta.schema === STORE_SCHEMA && meta.signOutGeneration >= generation) {
      return { from, epoch: meta.epoch };
    }
    for (const store of ALL_STORES) tx.objectStore(store).clear();
    const epoch = nextStoreEpoch(meta);
    putMeta(tx, { reader, epoch, signOutGeneration: generation, persisted: null });
    return { from, epoch };
  });
  // A wipe left unfinished is finished once this claim has emptied the store.
  void promise.then((claimed) => {
    if (claimed) settleWipePending(null);
  });
  claim = { reader, promise };
  void promise.then((claimed) => {
    if (claimed) void requestPersistence();
  });
  return promise;
}

const claimFor = (reader: string): Promise<Claimed | null> =>
  claim && claim.reader === reader ? claim.promise : Promise.resolve(null);

/** The reader a wipe may keep the store for: the one this document names. The
 *  wipe keeps it only if the store is still theirs. */
const keepableReader = (): string | null => getLocalSnapshotPrincipal();

/** Empty the store and advance its epoch, voiding every read in flight in any
 *  tab. With ``keepReader`` it stays claimed for that reader (an authorization
 *  change, a quota failure), but only if it was theirs; otherwise it belongs to
 *  no one until a reader is named again. */
async function wipe(keepReader: string | null): Promise<void> {
  pendingReads.clear();
  const pending = markWipePending();
  // A wipe that runs out of space gives up rather than wiping again. It stays
  // pending, and the next claim finishes it.
  const wiped = await transact('readwrite', ALL_STORES, async (tx) => {
    const meta = await getMeta(tx);
    for (const store of ALL_STORES) tx.objectStore(store).clear();
    putMeta(tx, {
      reader: keepReader && meta?.reader === keepReader ? keepReader : null,
      epoch: nextStoreEpoch(meta),
      signOutGeneration: readSignOutGeneration(),
      persisted: null,
    });
    return true;
  }, { onQuota: 'none' });
  if (wiped) settleWipePending(pending);
}

/** Empty the store for good: the reader's session ended (it expired). It stays
 *  unclaimed until a reader is named again. */
export function wipeLocalStore(): Promise<void> {
  return wipe(null);
}

subscribeLocalSnapshotPrincipal((next) => {
  claim = null;
  if (next) void claimReader(next);
});
if (getLocalSnapshotPrincipal()) void claimReader(getLocalSnapshotPrincipal()!);
// Every whole-store clear of the snapshots is a clear of this store too, awaited
// by whoever cleared, so sign-out does not navigate away before it lands. A
// sign-out or revocation unnames the reader first, so the store is left
// unclaimed; an authorization change or a quota failure keeps the reader.
registerLocalSnapshotClearHook(() => wipe(keepableReader()));
// A sign-out in another tab, including one running an older build that does not
// know this store exists, bumps the shared generation. Empty the store now
// rather than at the next open.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key === SIGN_OUT_GENERATION_KEY) void wipe(null);
  });
}

/** What the store looked like when a read started. Its answer may be written
 *  only while all of it still holds. */
export type LocalStoreTicket = {
  readonly sessionId: string;
  readonly reader: string | null;
  readonly storeEpoch: number;
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
    storeEpoch: readStoreEpoch(),
    observedAt,
    streamToken: currentStreamToken(),
  };
}

const readPending = (sessionId: string) =>
  Date.now() - (pendingReads.get(sessionId) ?? -Infinity) <= LOCAL_STORE_MAX_ANSWER_AGE_MS;

/** The reader a ticket's answer belongs to, or null when it may not be stored.
 *  A read that started before any reader was named belongs to the one named
 *  since. Wipes, sign-outs, and forgets in any tab are checked inside the
 *  transaction that writes it (``metaAdmits``, ``forgottenAt``). */
function ticketReader(ticket: LocalStoreTicket): string | null {
  const reader = ticket.reader ?? getLocalSnapshotPrincipal();
  if (!reader || reader !== getLocalSnapshotPrincipal()) return null;
  if (Date.now() - ticket.observedAt > LOCAL_STORE_MAX_ANSWER_AGE_MS) return null;
  return reader;
}

/** The reader and store epoch to write a ticket's answer under, once this
 *  document's claim for that reader has landed; null when it may not be
 *  written. Any wipe since the read started, in any tab, has moved the epoch. */
async function writerFor(ticket: LocalStoreTicket): Promise<{ reader: string; epoch: number } | null> {
  const reader = ticketReader(ticket);
  if (!reader) return null;
  const claimed = await claimFor(reader);
  if (!claimed || ticketReader(ticket) !== reader) return null;
  return { reader, epoch: ticket.storeEpoch === claimed.from ? claimed.epoch : ticket.storeEpoch };
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
async function putRow(
  { tx, sessionDelta }: RowWrite,
  row: WorkbenchMessage,
  sessionId: string,
  observedAt: number,
  watermarks: readonly Watermark[] = [],
): Promise<boolean> {
  const messages = tx.objectStore(MESSAGES);
  const tombstones = tx.objectStore(TOMBSTONES);
  const [existing, tombstone] = await Promise.all([
    settle(messages.get(row.id) as IDBRequest<StoredMessage | undefined>),
    settle(tombstones.get(row.id) as IDBRequest<Tombstone | undefined>),
  ]);
  if ((existing && existing.observedAt > observedAt) || (tombstone && tombstone.observedAt > observedAt)) return false;
  if (!existing && !tombstone) {
    const key = orderKeyOf(row);
    if (watermarks.some((mark) => mark.observedAt > observedAt && withinReach(key, mark.low, mark.high))) return false;
  }
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
  const writer = await writerFor(ticket);
  if (!writer) return;
  const { sessionId, observedAt } = ticket;
  const now = Date.now();
  await transact('readwrite', [META, SESSIONS, MESSAGES, RANGES, TOMBSTONES], async (tx) => {
    if (!(await metaAdmits(tx, writer.reader, writer.epoch))) return;
    const rangesStore = tx.objectStore(RANGES);
    const current = await settle(rangesStore.get(sessionId) as IDBRequest<StoredRanges | undefined>);
    // Forgotten, in this tab or another, after this answer was read.
    if (current?.forgottenAt != null && current.forgottenAt >= observedAt) return;
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
    const watermarks = (current?.watermarks ?? []).filter((mark) => now - mark.observedAt <= LOCAL_STORE_MAX_ANSWER_AGE_MS);
    for (const row of stored) await putRow(write, row, sessionId, observedAt, watermarks);
    // A tail read across a stream gap, or while the stream is down, cannot
    // account for what the stream did not deliver, so its coverage gets no tail
    // claim and nothing streamed extends it.
    const streamToken = currentStreamToken();
    const tailClaim = streamLive && ticket.streamToken === streamToken;
    // A forget emptied the ranges, so what is here was written since.
    let ranges = mergePageCoverage(current?.ranges ?? [], reach, { streamToken, now, tailClaim });
    if (streamedAbove && tailClaim) ranges = extendTailCoverage(ranges, streamedAbove, streamToken);
    rangesStore.put({
      sessionId,
      ranges,
      watermarks: [...watermarks, { low: reach.low, high: reach.high, observedAt }],
      // Kept: an answer read before the forget may still be on its way.
      ...(current?.forgottenAt != null ? { forgottenAt: current.forgottenAt } : {}),
    } satisfies StoredRanges);
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
  const writer = await writerFor(ticket);
  if (!writer) return;
  const now = Date.now();
  await transact('readwrite', [META, SESSIONS, RANGES], async (tx) => {
    if (!(await metaAdmits(tx, writer.reader, writer.epoch))) return;
    const marker = await settle(tx.objectStore(RANGES).get(session.id) as IDBRequest<StoredRanges | undefined>);
    if (marker?.forgottenAt != null && marker.forgottenAt >= ticket.observedAt) return;
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
  const writer = await writerFor(ticket);
  if (!writer) return;
  const now = Date.now();
  await transact('readwrite', [META, SESSIONS, MESSAGES, RANGES, TOMBSTONES], async (tx) => {
    if (!(await metaAdmits(tx, writer.reader, writer.epoch))) return;
    const rangesStore = tx.objectStore(RANGES);
    const record = await settle(rangesStore.get(sessionId) as IDBRequest<StoredRanges | undefined>);
    const forgottenAt = record?.forgottenAt;
    // A row that arrived before the session's forget never lands.
    if (forgottenAt != null && ticket.observedAt <= forgottenAt) return;
    // Kept: it has coverage. A forget or an eviction empties the ranges.
    const current = record && record.ranges.length > 0 ? record : undefined;
    // A pending read admits the session's rows only if it came after any forget
    // of it in any tab.
    const admitted = readPending(sessionId)
      && (forgottenAt == null || (pendingReads.get(sessionId) ?? 0) > forgottenAt);
    const write: RowWrite = { tx, sessionDelta: new Map() };
    if (!current && !admitted) {
      // A session the store does not keep. The row may still have moved here
      // from one it keeps (``promote_suppressed_native_message`` rebinds rows):
      // the old copy leaves that session under the same P1 ordering.
      await removeMovedRow(write, row, sessionId, ticket.observedAt);
      await applySessionDelta(tx, write.sessionDelta, now);
      return;
    }
    if (!(await putRow(write, row, sessionId, ticket.observedAt))) return;
    if (current && streamLive && streamToken === currentStreamToken()) {
      rangesStore.put({ ...current, ranges: extendTailCoverage(current.ranges, orderKeyOf(row), streamToken) } satisfies StoredRanges);
    }
    await applySessionDelta(tx, write.sessionDelta, now);
  });
  scheduleBudgetCheck();
}

/** Remove the stored copy of ``row`` when it now belongs to ``sessionId``, a
 *  session the store does not keep, unless the stored copy is newer. */
async function removeMovedRow({ tx, sessionDelta }: RowWrite, row: WorkbenchMessage, sessionId: string, observedAt: number): Promise<void> {
  const messages = tx.objectStore(MESSAGES);
  const existing = await settle(messages.get(row.id) as IDBRequest<StoredMessage | undefined>);
  if (!existing || existing.sessionId === sessionId || existing.observedAt > observedAt) return;
  messages.delete(row.id);
  tx.objectStore(TOMBSTONES).put({ id: row.id, sessionId: existing.sessionId, observedAt } satisfies Tombstone);
  sessionDelta.set(existing.sessionId, (sessionDelta.get(existing.sessionId) ?? 0) - existing.bytes);
}

/** The event stream may have missed something: a new stream connected, a page
 *  resumed without proof its stream survived, or the controller leg recovered.
 *  No range keeps a tail claim across it. */
export function noteLocalStoreStreamGap(): void {
  streamEpoch += 1;
}

/** The event stream's connection state changed. Losing it ends every tail
 *  claim at once, before any reconnect: a page read while it is down must not
 *  join an older tail across rows the stream never delivered. Regaining it
 *  ends nothing; the reconnect's catch-up (``noteLocalStoreStreamGap``) does. */
export function noteLocalStoreStreamState(live: boolean): void {
  if (streamLive && !live) streamEpoch += 1;
  streamLive = live;
}

// ---------------------------------------------------------------------------
// Forgetting one session

/** Delete everything stored for ``sessionId`` and void every read of it still
 *  in flight: it was archived, left the reader's Inbox, or the server refused it. */
export async function forgetLocalStoreSession(sessionId: string): Promise<void> {
  // The marker this leaves also stops reads started before it, here or in any
  // tab, from admitting the session's streamed rows.
  await transact('readwrite', [SESSIONS, MESSAGES, RANGES, TOMBSTONES], async (tx) => {
    const messages = tx.objectStore(MESSAGES);
    const keys = await settle(messages.index(BY_SESSION).getAllKeys(sessionKeyRange(sessionId, null, null)));
    for (const key of keys) messages.delete(key);
    const tombstones = tx.objectStore(TOMBSTONES);
    const tombstoneKeys = await settle(tombstones.index(BY_SESSION).getAllKeys(IDBKeyRange.only(sessionId)));
    for (const key of tombstoneKeys) tombstones.delete(key);
    // The marker other tabs' reads of it are checked against.
    tx.objectStore(RANGES).put({ sessionId, ranges: [], forgottenAt: Date.now() } satisfies StoredRanges);
    tx.objectStore(SESSIONS).delete(sessionId);
  }, { onQuota: 'wipe' });
}

type SessionActivity = { session_id?: string | null; event?: string; visibility?: string; title?: string | null; pinned?: boolean };

/** A ``session.activity`` event. One that takes the Session out of the
 *  reader's Inbox (archived, or moved to the background) deletes it from the
 *  store. An update patches the stored row's title and pin, which decide how it
 *  is shown and whether eviction spares it. */
export function applyLocalStoreSessionActivity(data: SessionActivity): void {
  if (!data.session_id) return;
  if (data.event === 'archived' || data.visibility === 'background') {
    void forgetLocalStoreSession(data.session_id);
    return;
  }
  if (data.event === 'updated') void patchStoredSession(data.session_id, data);
}

async function patchStoredSession(sessionId: string, data: SessionActivity): Promise<void> {
  const patch: Partial<WorkbenchSession> = {};
  if (typeof data.pinned === 'boolean') patch.pinned = data.pinned;
  if (Object.prototype.hasOwnProperty.call(data, 'title')) patch.title = data.title ?? null;
  if (data.visibility === 'foreground' || data.visibility === 'system') patch.visibility = data.visibility;
  if (Object.keys(patch).length === 0) return;
  const ticket = ticketFor(sessionId, Date.now());
  const writer = await writerFor(ticket);
  if (!writer) return;
  await transact('readwrite', [META, SESSIONS], async (tx) => {
    if (!(await metaAdmits(tx, writer.reader, writer.epoch))) return;
    const sessions = tx.objectStore(SESSIONS);
    const stored = await settle(sessions.get(sessionId) as IDBRequest<StoredSession | undefined>);
    if (!stored?.row || stored.observedAt > ticket.observedAt) return;
    sessions.put({ ...stored, row: { ...stored.row, ...patch }, observedAt: ticket.observedAt } satisfies StoredSession);
  });
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
  }, { onQuota: 'wipe' });
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
    // Forget markers outlive every answer read before them, and no longer.
    const records = await settle(tx.objectStore(RANGES).getAll() as IDBRequest<StoredRanges[]>);
    for (const record of records) {
      if (record.forgottenAt == null || record.forgottenAt >= Date.now() - TOMBSTONE_TTL_MS) continue;
      // A stale marker goes; ranges written since it stay.
      if (record.ranges.length === 0) tx.objectStore(RANGES).delete(record.sessionId);
      else tx.objectStore(RANGES).put({ sessionId: record.sessionId, ranges: record.ranges, watermarks: record.watermarks } satisfies StoredRanges);
    }
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
        // Evicted like a forget: an answer read before now would put back older
        // copies of what was just removed, so the marker refuses it.
        tx.objectStore(RANGES).put({ sessionId: session.id, ranges: [], forgottenAt: Date.now() } satisfies StoredRanges);
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
  if (!reader || !(await claimFor(reader))) return null;
  const result = await transact('readonly', [META, SESSIONS, MESSAGES, RANGES], async (tx) => {
    const meta = await getMeta(tx);
    if (meta?.reader !== reader || meta.signOutGeneration !== readSignOutGeneration()) return null;
    const session = await settle(tx.objectStore(SESSIONS).get(sessionId) as IDBRequest<StoredSession | undefined>);
    const ranges = (await settle(tx.objectStore(RANGES).get(sessionId) as IDBRequest<StoredRanges | undefined>))?.ranges ?? [];
    const rows: StoredMessage[] = [];
    const upper = before ? IDBKeyRange.bound([sessionId], [sessionId, before.at, before.id], false, true) : sessionKeyRange(sessionId, null, null);
    await new Promise<void>((resolve, reject) => {
      const cursor = tx.objectStore(MESSAGES).index(BY_SESSION).openCursor(upper, 'prev');
      cursor.onsuccess = () => {
        const current = cursor.result;
        // One past the limit says whether anything older is stored.
        if (!current || rows.length > limit) {
          resolve();
          return;
        }
        rows.push(current.value as StoredMessage);
        current.continue();
      };
      cursor.onerror = () => reject(cursor.error);
    });
    const olderStored = rows.length > limit;
    if (olderStored) rows.length = limit;
    rows.reverse();
    const token = currentStreamToken();
    const contiguous = rows.length > 0
      && ranges.some((range) => isCovered([range], orderKeyOf(rows[0].row), token) && isCovered([range], orderKeyOf(rows[rows.length - 1].row), token));
    const startReached = rows.length > 0 && !olderStored
      && ranges.some((range) => range.from === null && isCovered([range], orderKeyOf(rows[0].row), token));
    return {
      session: session?.row ?? null,
      messages: rows.map((row) => row.row),
      contiguous,
      startReached,
    } satisfies LocalStoreWindow;
  });
  if (!result || getLocalSnapshotPrincipal() !== reader) return null;
  return result;
}
