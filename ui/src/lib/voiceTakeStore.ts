// Voice takes kept on this device until their words land (docs/plans/voice-input-reliability.md,
// "Kept takes"). A take's audio, as the 16 kHz mono WAV segments the recorder already produced,
// is written here while it records, and deleted once its text has been inserted or sent, when
// the reader discards it, and on sign-out or a change of reader. A reload, a crash, or a page
// the system killed therefore loses no speech: the take comes back as a pending voice item near
// the composer it was spoken into, to be retried or discarded.
//
// The rules follow the local snapshots' (lib/localSnapshots), through their hooks:
//  * Takes belong to one reader principal. Nothing is read or written while it is unknown, a
//    take stored for another principal is never returned, and naming a principal deletes every
//    other principal's takes.
//  * A sign-out or a revocation (which advance the shared sign-out generation, then clear the
//    snapshots) deletes every take, and the sign-out waits for it (the snapshots' clear hook).
//    A sign-out in another tab voids them here too (the generation's storage event, and the
//    generation every take is stored under).
//  * Unlike the snapshots, takes are not server reads but the reader's own unsent speech: a
//    clear for an authorization change or a full quota, or an expired session the same reader
//    signs back into, leaves them. A different reader signing in still deletes them.
//  * Best effort: a missing, blocked, or full database means the take lives in memory only, as
//    it did before this store; nothing here throws to a caller.
//
// Bounded: at most VOICE_TAKE_LIMIT takes per reader (the oldest goes first); a minute of audio
// is about 1.9 MB, so even long takes stay a few tens of MB.
//
// The interface is deliberately narrow (save, read, delete, subscribe) so the module can move
// under the local store (lib/localStore) without changing its callers.

import {
  getLocalSnapshotPrincipal,
  registerLocalSnapshotClearHook,
  subscribeLocalSnapshotPrincipal,
} from './localSnapshots';
import { readSignOutGeneration, SIGN_OUT_GENERATION_KEY } from './signOutGeneration';

const DB_NAME = 'avibe-voice-takes';
const DB_VERSION = 1;
const TAKES = 'takes';
const SEGMENTS = 'segments';

/** The most takes kept per reader; a new one beyond it evicts the oldest. */
export const VOICE_TAKE_LIMIT = 3;

/** How a take's words were meant to land: sent as a message (a hold) or put into a draft
 *  (a tap or ⌥Z dictation). */
export type VoiceTakeIntent = 'send' | 'insert';

export type VoiceTakeSegmentInfo = {
  sequence: number;
  final: boolean;
  durationMs: number;
  overlapMs?: number;
  /** A finalize-only marker: the recording ended exactly at a segment boundary. */
  empty?: boolean;
};

export type VoiceTake = {
  /** The dictation id. */
  id: string;
  /** Where its words go: a Session id, or `home` for the Home composer's draft. */
  target: string;
  intent: VoiceTakeIntent;
  /** Who it was spoken to, as the card said it ("To Vibey"); shown with the kept take. */
  label: string | null;
  createdAt: number;
  updatedAt: number;
  /** recording: still being captured · pending: no text yet · transcribed: text that has not
   *  landed yet. */
  status: 'recording' | 'pending' | 'transcribed';
  text?: string;
  /** Some recorded audio never reached the device (the page died between a segment and its
   *  bytes): `text` holds what was kept, up to the first missing part. */
  truncated?: boolean;
  segments: VoiceTakeSegmentInfo[];
  /** How many times its record was written; set by the store. A writer that is not the take's
   *  keeper (a retry from another surface) updates only the revision it read. */
  revision?: number;
};

type StoredTake = VoiceTake & { principal: string; generation: number };
type StoredSegment = { id: [string, number]; take: string; audio: ArrayBuffer | null };

export const voiceTakeDurationMs = (take: Pick<VoiceTake, 'segments'>): number => {
  const ordered = [...take.segments].sort((left, right) => left.sequence - right.sequence);
  return ordered.reduce((total, segment) => total + Math.max(0, segment.durationMs - (segment.overlapMs ?? 0)), 0);
};

let dbPromise: Promise<IDBDatabase | null> | null = null;
const listeners = new Set<() => void>();
// The sign-out generation when this document last named its reader: a write is fenced to it, so
// a sign-out in any tab stops every writer here until the reader is named again.
let principalGeneration = readGeneration();
// The generation whose sign-out this document has already wiped for.
let wipedGeneration = principalGeneration;

function readGeneration(): number {
  try {
    return readSignOutGeneration();
  } catch {
    return 0;
  }
}

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
      if (!db.objectStoreNames.contains(TAKES)) db.createObjectStore(TAKES, { keyPath: 'id' });
      if (!db.objectStoreNames.contains(SEGMENTS)) {
        db.createObjectStore(SEGMENTS, { keyPath: 'id' }).createIndex('byTake', 'take');
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

async function withStores<T>(
  mode: IDBTransactionMode,
  run: (takes: IDBObjectStore, segments: IDBObjectStore) => Promise<T> | T,
): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  try {
    const tx = db.transaction([TAKES, SEGMENTS], mode);
    const result = await run(tx.objectStore(TAKES), tx.objectStore(SEGMENTS));
    await done(tx);
    return result;
  } catch {
    return null;
  }
}

const notifyHere = () => {
  for (const listener of listeners) {
    try {
      listener();
    } catch {
      // One listener cannot stop the others.
    }
  }
};

// Other documents of this origin (the pet window, another tab) hear about every change too.
const channel = (() => {
  try {
    return typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(DB_NAME);
  } catch {
    return null;
  }
})();
if (channel) channel.onmessage = () => notifyHere();

const notify = () => {
  notifyHere();
  try {
    channel?.postMessage('changed');
  } catch {
    // A closed channel only costs the other documents a later read.
  }
};

function deleteTakeIn(takes: IDBObjectStore, segments: IDBObjectStore, id: string): void {
  takes.delete(id);
  segments.delete(IDBKeyRange.bound([id, 0], [id, Number.MAX_SAFE_INTEGER]));
}

/** Whether a stored take may be read or written by the current reader. */
const owned = (record: StoredTake | undefined, owner: string): record is StoredTake => (
  record !== undefined && record.principal === owner && record.generation === readGeneration()
);

/** Who may write, and under which sign-out generation: a writer holds this from when its take
 *  begins, and every write it makes must still match it. */
export type VoiceTakeFence = { principal: string; generation: number };

/** The current reader's fence, or null while the reader is unknown or after a sign-out (in any
 *  tab) since this document last named it. */
export function voiceTakeFence(): VoiceTakeFence | null {
  const principal = getLocalSnapshotPrincipal();
  const generation = readGeneration();
  if (!principal || principalGeneration !== generation) return null;
  return { principal, generation };
}

/** The fence's reader, while the fence still holds. */
function writer(fence: VoiceTakeFence | null = voiceTakeFence()): string | null {
  const current = voiceTakeFence();
  if (!fence || !current || current.principal !== fence.principal || current.generation !== fence.generation) return null;
  return fence.principal;
}

/**
 * Stores a take's details (not its audio). A new take beyond `VOICE_TAKE_LIMIT` evicts the
 * oldest the reader keeps. Resolves false when nothing could be stored.
 */
export async function saveVoiceTake(take: VoiceTake, fence?: VoiceTakeFence | null): Promise<boolean> {
  const owner = writer(fence === undefined ? voiceTakeFence() : fence);
  if (!owner) return false;
  const generation = readGeneration();
  const saved = await withStores('readwrite', async (takes, segments) => {
    const all = (await settle(takes.getAll() as IDBRequest<StoredTake[]>)).filter((record) => owned(record, owner));
    if (!all.some((record) => record.id === take.id)) {
      const others = all.sort((left, right) => left.createdAt - right.createdAt);
      for (const old of others.slice(0, Math.max(0, others.length - (VOICE_TAKE_LIMIT - 1)))) {
        deleteTakeIn(takes, segments, old.id);
      }
    }
    const existing = all.find((record) => record.id === take.id);
    takes.put({ ...take, principal: owner, generation, revision: (existing?.revision ?? 0) + 1 } satisfies StoredTake);
    return true;
  });
  if (saved) notify();
  return saved === true;
}

/** Updates a take the reader still keeps; one deleted meanwhile (discarded, landed) stays
 *  deleted. With `ifRevision`, only while the stored take is still at that revision: a take its
 *  keeper advanced meanwhile (more segments, its own text) is never set back. Resolves false
 *  when nothing was updated. */
export async function updateVoiceTake(take: VoiceTake, fence?: VoiceTakeFence | null, ifRevision?: number): Promise<boolean> {
  const owner = writer(fence === undefined ? voiceTakeFence() : fence);
  if (!owner) return false;
  const updated = await withStores('readwrite', async (takes) => {
    const record = await settle(takes.get(take.id) as IDBRequest<StoredTake | undefined>);
    if (!owned(record, owner)) return false;
    const revision = record.revision ?? 0;
    if (ifRevision !== undefined && revision !== ifRevision) return false;
    takes.put({ ...take, principal: owner, generation: record.generation, revision: revision + 1 } satisfies StoredTake);
    return true;
  });
  if (updated) notify();
  return updated === true;
}

/** Stores one segment's audio (the WAV bytes, or null for a finalize-only marker). */
export async function saveVoiceTakeAudio(
  id: string,
  sequence: number,
  audio: Blob | null,
  fence?: VoiceTakeFence | null,
): Promise<boolean> {
  const owner = writer(fence === undefined ? voiceTakeFence() : fence);
  if (!owner) return false;
  // Bytes, not the Blob: a Blob held across a suspended page may not be readable on iOS.
  const bytes = audio ? await audio.arrayBuffer().catch(() => null) : null;
  if (audio && !bytes) return false;
  const saved = await withStores('readwrite', async (takes, segments) => {
    // Checked again here: a sign-out may have landed while the bytes were read.
    if (writer(fence === undefined ? voiceTakeFence() : fence) !== owner) return false;
    const record = await settle(takes.get(id) as IDBRequest<StoredTake | undefined>);
    if (!owned(record, owner)) return false;
    segments.put({ id: [id, sequence], take: id, audio: bytes } satisfies StoredSegment);
    return true;
  });
  return saved === true;
}

/** The reader's kept takes, oldest first; only those for `target` when it is given. */
export async function readVoiceTakes(target?: string): Promise<VoiceTake[]> {
  const owner = getLocalSnapshotPrincipal();
  if (!owner) return [];
  const records = await withStores('readonly', (takes) => settle(takes.getAll() as IDBRequest<StoredTake[]>));
  return (records ?? [])
    .filter((record) => owned(record, owner) && (target === undefined || record.target === target))
    .sort((left, right) => left.createdAt - right.createdAt)
    .map(({ principal: _principal, generation: _generation, ...take }) => take);
}

/** A kept take's audio, as fresh Blobs (one per stored segment, by sequence). */
export async function readVoiceTakeAudio(id: string): Promise<Map<number, Blob | null>> {
  const audio = new Map<number, Blob | null>();
  const owner = getLocalSnapshotPrincipal();
  if (!owner) return audio;
  const found = await withStores('readonly', async (takes, segments) => {
    const record = await settle(takes.get(id) as IDBRequest<StoredTake | undefined>);
    if (!owned(record, owner)) return [];
    return settle(segments.index('byTake').getAll(id) as IDBRequest<StoredSegment[]>);
  });
  for (const segment of found ?? []) {
    audio.set(segment.id[1], segment.audio ? new Blob([segment.audio], { type: 'audio/wav' }) : null);
  }
  return audio;
}

/** Deletes a take and its audio: its words landed, or the reader discarded it. */
export async function deleteVoiceTake(id: string): Promise<void> {
  const removed = await withStores('readwrite', (takes, segments) => {
    deleteTakeIn(takes, segments, id);
    return true;
  });
  if (removed) notify();
}

// Takes a surface of this document holds in memory right now (a card shows it, a composer
// retains it, a retry is sending it). The pending voice items leave these to their owner.
const live = new Set<string>();

/** Marks a take as held by a live surface of this document, or no longer. */
export function setVoiceTakeLive(id: string, held: boolean): void {
  if (held === live.has(id)) return;
  if (held) live.add(id);
  else live.delete(id);
  notify();
}

export function isVoiceTakeLive(id: string): boolean {
  return live.has(id);
}

/** Called whenever the kept takes change in this document. */
export function subscribeVoiceTakes(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

async function deleteWhere(keep: (record: StoredTake) => boolean): Promise<void> {
  const removed = await withStores('readwrite', async (takes, segments) => {
    const records = await settle(takes.getAll() as IDBRequest<StoredTake[]>);
    let count = 0;
    for (const record of records) {
      if (keep(record)) continue;
      deleteTakeIn(takes, segments, record.id);
      count += 1;
    }
    return count;
  });
  if (removed) notify();
}

// The reader's changes: a different reader deletes every other reader's takes; a sign-out or
// revocation (which advances the shared sign-out generation and clears the reader) deletes all.
let wipe: Promise<void> = Promise.resolve();

/** Deletes every take stored before a sign-out the shared generation shows, once. Writers stay
 *  fenced until the reader is named again. */
function noteSignOut(): Promise<void> {
  const generation = readGeneration();
  if (generation !== wipedGeneration) {
    wipedGeneration = generation;
    wipe = deleteWhere((record) => record.generation === generation);
  }
  return wipe;
}

let watching = false;
function watchReader(): void {
  if (watching || typeof window === 'undefined') return;
  watching = true;
  // The sign-out waits for this before it navigates away. An authorization change or a full
  // quota clears the snapshots without moving the generation, and leaves the takes.
  registerLocalSnapshotClearHook(noteSignOut);
  subscribeLocalSnapshotPrincipal((next) => {
    const signedOut = readGeneration() !== wipedGeneration ? noteSignOut() : Promise.resolve();
    if (!next) {
      void signedOut.then(notify);
      return;
    }
    // A reader named (again): writes are allowed under the generation of now.
    principalGeneration = readGeneration();
    // Readers that read while the reader was unknown found nothing: they read again.
    void signedOut
      .then(() => deleteWhere((record) => record.principal === next))
      .finally(notify);
  });
  window.addEventListener('storage', (event) => {
    if (event.key === SIGN_OUT_GENERATION_KEY) void noteSignOut();
  });
}
watchReader();
