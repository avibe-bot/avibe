// Last-known snapshots of server reads, kept in IndexedDB so a cold start can
// paint the Inbox (and later the sidebar and conversations) before the network
// answers. docs/plans/web-ui-local-cache.md owns the design; the rules that
// matter here:
//
//  * The server stays authoritative. A snapshot only fills the screen until the
//    live read lands, and the live read replaces it.
//  * Every operation is best-effort. A missing, evicted, corrupt or oversized
//    snapshot means today's behavior, so nothing here throws to a caller.
//  * Snapshots belong to one reader principal (``local`` or ``remote:<sub>``,
//    the ``author_id`` of ``/api/session``). Nothing is read or written while the
//    principal is unknown, a record written for another principal is never
//    returned, and setting a principal deletes every other principal's records.
//
// IndexedDB is per origin, and every Avibe instance has its own origin, so
// records of different instances never meet.

const DB_NAME = 'avibe-local-snapshots';
const DB_VERSION = 1;
const STORE = 'snapshots';

/** A record larger than this (serialized, in bytes) is not worth a cold start's
 *  read; it is dropped instead of stored. */
export const LOCAL_SNAPSHOT_MAX_BYTES = 512 * 1024;

type StoredRecord = {
  id: string;
  principal: string;
  key: string;
  schema: number;
  savedAt: number;
  value: unknown;
};

let principal: string | null = null;
const principalListeners = new Set<(next: string | null) => void>();
let dbPromise: Promise<IDBDatabase | null> | null = null;

const recordId = (owner: string, key: string) => `${owner}\n${key}`;

function idbFactory(): IDBFactory | null {
  try {
    return typeof indexedDB === 'undefined' ? null : indexedDB;
  } catch {
    // Some embedded or privacy modes throw on access instead of omitting it.
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
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'id' });
    };
    request.onsuccess = () => {
      const db = request.result;
      // Another tab upgrading the schema must not be blocked by this one.
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

function transactionDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => Promise<T> | T,
): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  try {
    const tx = db.transaction(STORE, mode);
    const result = await run(tx.objectStore(STORE));
    await transactionDone(tx);
    return result;
  } catch (error) {
    if (isQuotaError(error)) await clearLocalSnapshots();
    return null;
  }
}

function isQuotaError(error: unknown): boolean {
  return error instanceof DOMException
    && (error.name === 'QuotaExceededError' || error.name === 'NS_ERROR_DOM_QUOTA_REACHED');
}

function serializedBytes(value: unknown): number | null {
  try {
    const json = JSON.stringify(value);
    return json === undefined ? null : new TextEncoder().encode(json).length;
  } catch {
    return null;
  }
}

/** The reader principal snapshots are stored for, or null while unknown. */
export function getLocalSnapshotPrincipal(): string | null {
  return principal;
}

/** Called by the authorization boundary once ``/api/session`` names the reader,
 *  and with null when it no longer does. Setting a principal deletes every
 *  record that belongs to anyone else: a different person signing in on this
 *  device must never see the last person's snapshots. */
export function setLocalSnapshotPrincipal(next: string | null): void {
  const normalized = next || null;
  if (normalized === principal) return;
  principal = normalized;
  if (normalized) void deleteOtherPrincipals(normalized);
  for (const listener of principalListeners) listener(normalized);
}

export function subscribeLocalSnapshotPrincipal(listener: (next: string | null) => void): () => void {
  principalListeners.add(listener);
  return () => {
    principalListeners.delete(listener);
  };
}

async function deleteOtherPrincipals(owner: string): Promise<void> {
  await withStore('readwrite', async (store) => {
    const records = await settle(store.getAll() as IDBRequest<StoredRecord[]>);
    for (const record of records) {
      if (record.principal !== owner) store.delete(record.id);
    }
  });
}

/** The current principal's snapshot for ``key``, or null when there is none, it
 *  was written under another schema, or ``parse`` rejects it. */
export async function readLocalSnapshot<T>(
  key: string,
  schema: number,
  parse: (value: unknown) => T | null,
): Promise<T | null> {
  const owner = principal;
  if (!owner) return null;
  const epoch = clearEpoch;
  const record = await withStore('readonly', (store) =>
    settle(store.get(recordId(owner, key)) as IDBRequest<StoredRecord | undefined>),
  );
  // The principal may have changed while the read was in flight, and a clear
  // that began meanwhile voids what the read found.
  if (!record || owner !== principal || epoch !== clearEpoch) return null;
  if (record.principal !== owner || record.schema !== schema) return null;
  try {
    return parse(record.value);
  } catch {
    return null;
  }
}

/** Store ``value`` as the current principal's snapshot for ``key``. A value too
 *  large to be worth a cold-start read deletes the stale record instead. */
export async function writeLocalSnapshot(key: string, schema: number, value: unknown): Promise<void> {
  const owner = principal;
  if (!owner) return;
  const bytes = serializedBytes(value);
  await withStore('readwrite', (store) => {
    if (owner !== principal) return;
    if (bytes === null || bytes > LOCAL_SNAPSHOT_MAX_BYTES) {
      store.delete(recordId(owner, key));
      return;
    }
    const record: StoredRecord = { id: recordId(owner, key), principal: owner, key, schema, savedAt: Date.now(), value };
    store.put(record);
  });
}

/** Stop storing for the current reader, then delete every snapshot. Sign-out and
 *  revocation use this: a write still scheduled for the reader (a debounce timer,
 *  a page being hidden) must not put back what was just removed. */
export async function forgetLocalSnapshots(): Promise<void> {
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
}

/** Delete every snapshot. Used when an authorization change voids what was
 *  cached, and when storage runs out of quota. */
export async function clearLocalSnapshots(): Promise<void> {
  clearEpoch += 1;
  for (const listener of epochListeners) listener();
  const db = await openDb();
  if (!db) return;
  try {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).clear();
    await transactionDone(tx);
  } catch {
    // Nothing more can be done; a later write overwrites what is left.
  }
}

// Advanced by every whole-store clear (an authorization change, sign-out,
// revocation, a quota failure). A writer that holds data read before a clear
// compares the value it noted then with the current one, so a write it still
// has scheduled cannot put back what the clear removed.
let clearEpoch = 0;
const epochListeners = new Set<() => void>();

/** The number of whole-store clears so far in this document. */
export function getLocalSnapshotEpoch(): number {
  return clearEpoch;
}

/** Called on every whole-store clear: what was painted from the store is void
 *  too, whoever still shows it. */
export function subscribeLocalSnapshotEpoch(listener: () => void): () => void {
  epochListeners.add(listener);
  return () => {
    epochListeners.delete(listener);
  };
}

/** Delete ``key`` for every reader. Used when the server says the record's
 *  subject is gone or no longer visible, which holds whoever stored it. */
export async function deleteLocalSnapshot(key: string): Promise<void> {
  await withStore('readwrite', async (store) => {
    const ids = await settle(store.getAllKeys());
    for (const id of ids) {
      if (typeof id === 'string' && id.endsWith(`\n${key}`)) store.delete(id);
    }
  });
}

/** Keep the ``keep`` most recently saved records whose key starts with
 *  ``prefix`` and delete the rest. This reads every such record, so call it when
 *  a write may have added one, not on every write. */
export async function pruneLocalSnapshots(prefix: string, keep: number): Promise<void> {
  await withStore('readwrite', async (store) => {
    const records = await settle(store.getAll() as IDBRequest<StoredRecord[]>);
    const matching = records
      .filter((record) => typeof record.key === 'string' && record.key.startsWith(prefix))
      .sort((a, b) => (b.savedAt ?? 0) - (a.savedAt ?? 0));
    for (const record of matching.slice(Math.max(0, keep))) store.delete(record.id);
  });
}
