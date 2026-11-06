// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const platform = vi.hoisted(() => ({ standalone: false, ios: false }));
vi.mock('../platform', () => ({ isStandalonePwa: () => platform.standalone, isIosDevice: () => platform.ios }));

import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { clearLocalSnapshots, forgetLocalSnapshots, setLocalSnapshotPrincipal } from '../localSnapshots';
import { SIGN_OUT_GENERATION_KEY, bumpSignOutGeneration } from '../signOutGeneration';
import {
  beginLocalStoreRead,
  forgetLocalStoreSession,
  applyLocalStoreSessionActivity,
  noteLocalStoreStreamGap,
  noteLocalStoreStreamState,
  readLocalStoreMediaMeta,
  readLocalStoreAround,
  readLocalStoreOlder,
  readLocalStoreWindow,
  recordLocalStoreMediaMeta,
  recordLocalStorePage,
  recordLocalStoreStreamRow,
  wipeLocalStore,
} from '.';

// The local message store keeps a reader's private history on the device
// (docs/plans/local-sync-store.md). These cases are the rules that keep it
// correct (P1, P2) and keep it from outliving the reader it belongs to.

const at = (n: number) => `2026-11-05T00:${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}.000000Z`;
const row = (n: number, extra: Partial<WorkbenchMessage> = {}, sessionId = 'ses_a'): WorkbenchMessage => ({
  // Message ids are unique across sessions, as the server's are.
  id: sessionId === 'ses_a' ? `msg_${String(n).padStart(3, '0')}` : `msg_${sessionId}_${String(n).padStart(3, '0')}`,
  scope_id: null,
  session_id: sessionId,
  platform: 'avibe',
  author: 'agent',
  type: 'result',
  source: 'agent',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: `row ${n}`,
  content: {},
  metadata: {},
  created_at: at(n),
  updated_at: at(n),
  delivered_at: null,
  read_at: null,
  ...extra,
});
const rows = (from: number, to: number, sessionId = 'ses_a') =>
  Array.from({ length: to - from + 1 }, (_, i) => row(from + i, {}, sessionId));
const session = (id = 'ses_a', extra: Partial<WorkbenchSession> = {}) =>
  ({ id, title: id, status: 'active', pinned: false, last_active_at: at(1), updated_at: at(1), ...extra }) as WorkbenchSession;

const ids = async (sessionId = 'ses_a') =>
  ((await readLocalStoreWindow(sessionId, { limit: 500 }))?.messages ?? []).map((message) => Number(message.id.slice(-3)));
const texts = async (sessionId = 'ses_a') =>
  ((await readLocalStoreWindow(sessionId, { limit: 500 }))?.messages ?? []).map((message) => message.text);
const tail = (messages: WorkbenchMessage[], next_before_id: string | null = 'cursor', observedAt = Date.now(), sessionId = 'ses_a') =>
  recordLocalStorePage(beginLocalStoreRead(sessionId, observedAt), { kind: 'tail' }, { messages, next_before_id }, session(sessionId));
// Hand-offs run on their own; under a full parallel suite they can
// take well over vitest's one-second default to land.
const SETTLE = { timeout: 10_000, interval: 25 };
/** When ``sessionId`` was last forgotten (or evicted), read past every fence. */
const forgottenAt = (sessionId: string) => new Promise<number | null>((resolve, reject) => {
  const open = indexedDB.open('avibe-local-store');
  open.onerror = () => reject(open.error);
  open.onsuccess = () => {
    const db = open.result;
    const read = db.transaction('ranges', 'readonly').objectStore('ranges').get(sessionId);
    read.onsuccess = () => {
      db.close();
      resolve((read.result as { forgottenAt?: number } | undefined)?.forgottenAt ?? null);
    };
    read.onerror = () => reject(read.error);
  };
});
// A forget an event starts runs on its own: wait for it to land, not for a time.
const forgetLanded = (sessionId: string) => vi.waitFor(async () => expect(await forgottenAt(sessionId)).not.toBeNull(), SETTLE);
// The store orders a read after a forget only when it started in a later
// millisecond: one from the same millisecond cannot be told apart from one that
// started before it, and is refused. A case about a read after the forget
// starts it once the clock has passed the forget.
const pastForget = async (sessionId: string) => {
  const at = await forgottenAt(sessionId);
  await vi.waitFor(() => expect(Date.now()).toBeGreaterThan(at ?? Number.POSITIVE_INFINITY), SETTLE);
};
/** Rows on disk, read past every fence: what a wipe must have removed. */
const rowsOnDisk = () => new Promise<number>((resolve, reject) => {
  const open = indexedDB.open('avibe-local-store');
  open.onerror = () => reject(open.error);
  open.onsuccess = () => {
    const db = open.result;
    const count = db.transaction('messages', 'readonly').objectStore('messages').count();
    count.onsuccess = () => {
      db.close();
      resolve(count.result);
    };
    count.onerror = () => reject(count.error);
  };
});

beforeEach(() => {
  setLocalSnapshotPrincipal('remote:alice');
  // Most cases run on a connected stream; the outage cases say otherwise.
  noteLocalStoreStreamState(true);
});

afterEach(async () => {
  platform.standalone = false;
  platform.ios = false;
  vi.unstubAllGlobals();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
  window.localStorage.clear();
});

describe('P2: a page speaks only for what it proves', () => {
  it('lets a tail replace everything above its first row, including a newest row the server no longer has', async () => {
    await tail(rows(1, 5));
    // Row 5 moved to another session since; row 1 is older than this tail.
    await tail(rows(2, 4));
    expect(await ids()).toEqual([1, 2, 3, 4]);
  });

  it('lets a page before a cursor replace only between its own first and last rows', async () => {
    await tail([...rows(1, 3), ...rows(5, 9)]);
    const older = beginLocalStoreRead('ses_a');
    // The server returns rows 2 and 4 to 6; rows 1 and 7 to 9 lie outside what it proved.
    await recordLocalStorePage(older, { kind: 'before' }, { messages: [row(2), row(4), row(5), row(6)], next_before_id: 'msg_002' });
    expect(await ids()).toEqual([1, 2, 4, 5, 6, 7, 8, 9]);
  });

  it('applies nothing from a window whose anchor the server could not resolve', async () => {
    await tail(rows(1, 5));
    const around = beginLocalStoreRead('ses_a');
    await recordLocalStorePage(around, { kind: 'around' }, { messages: [], next_before_id: null, next_after_id: null });
    expect(await ids()).toEqual([1, 2, 3, 4, 5]);
  });

  it('does not let a claimed-input projection on a tail widen what the tail replaces', async () => {
    await tail(rows(1, 10), null);
    const projection = row(2, { id: 'delivery_x', projection: 'claimed_delivery', type: 'user' });
    await tail([...rows(8, 10), projection]);
    expect(await ids()).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });

  it('empties a session the server answers as empty from both ends', async () => {
    await tail(rows(1, 3), null);
    await tail([], null);
    expect(await ids()).toEqual([]);
  });

  it('gives no tail claim to a tail read that started before a stream gap', async () => {
    const slow = beginLocalStoreRead('ses_a');
    noteLocalStoreStreamGap();
    await recordLocalStorePage(slow, { kind: 'tail' }, { messages: rows(1, 5), next_before_id: null }, session());
    // Row 6 may have been dropped in the gap; a later streamed row must not be
    // joined to the tail across it.
    await recordLocalStoreStreamRow(row(7));
    expect((await readLocalStoreWindow('ses_a'))?.contiguous).toBe(false);
  });

  it('ends every tail claim when the stream drops, and grants none to pages read while it is down', async () => {
    await tail(rows(1, 50), null);
    noteLocalStoreStreamState(false);
    // Rows 51-70 arrived on the server; this tail was read during the outage.
    await tail(rows(71, 100));
    expect((await readLocalStoreWindow('ses_a', { limit: 60 }))?.contiguous).toBe(false);
    // A second tail read during the same outage does not join the first either.
    await tail(rows(111, 120));
    expect((await readLocalStoreWindow('ses_a', { limit: 15 }))?.contiguous).toBe(false);
  });

  it('reports the start of history only when the window reaches it', async () => {
    await tail(rows(1, 100), null);
    expect((await readLocalStoreWindow('ses_a', { limit: 50 }))?.startReached).toBe(false);
    expect((await readLocalStoreWindow('ses_a', { limit: 200 }))?.startReached).toBe(true);
  });

  it('keeps two tails separated by a disconnect apart, leaving the gap visible', async () => {
    await tail(rows(1, 50), null);
    noteLocalStoreStreamGap();
    await tail(rows(71, 120));
    const newest = await readLocalStoreWindow('ses_a', { limit: 50 });
    expect(newest?.contiguous).toBe(true);
    const across = await readLocalStoreWindow('ses_a', { limit: 60 });
    expect(across?.messages.map((message) => message.id)).toContain('msg_050');
    expect(across?.contiguous).toBe(false);
  });
});

describe('reading history next to what the reader has', () => {
  // A chat shows stored rows at once only where the store holds the stretch in
  // full, so nothing can be missing between them and what is on screen.
  const num = (messages: WorkbenchMessage[] | undefined) => (messages ?? []).map((message) => Number(message.id.slice(-3)));

  it('gives the page just older than a row, joined to it, and says whether more is stored or history starts', async () => {
    await tail(rows(1, 60), null);
    const page = await readLocalStoreOlder('ses_a', row(51), 20);
    expect(num(page?.messages)).toEqual(rows(31, 50).map((_, i) => 31 + i));
    expect([page?.moreStored, page?.startReached]).toEqual([true, false]);
    const last = await readLocalStoreOlder('ses_a', row(31), 50);
    expect(num(last?.messages)).toEqual(rows(1, 30).map((_, i) => 1 + i));
    expect([last?.moreStored, last?.startReached]).toEqual([false, true]);
  });

  it('stops at a gap the store cannot account for, and gives nothing for a row on no full stretch', async () => {
    await tail(rows(1, 50), null);
    noteLocalStoreStreamGap();
    await tail(rows(71, 120));
    // Rows 71 to 120 were proved from row 71 up; what lies below is not known here.
    const atGap = await readLocalStoreOlder('ses_a', row(80), 50);
    expect(num(atGap?.messages)).toEqual(rows(71, 79).map((_, i) => 71 + i));
    expect([atGap?.moreStored, atGap?.startReached]).toEqual([false, false]);
    expect((await readLocalStoreOlder('ses_a', row(71), 50))?.messages).toEqual([]);
    expect((await readLocalStoreOlder('ses_a', row(60), 50))?.messages).toEqual([]);
  });

  it('gives a window around a stored row, and nothing for one the store does not hold in full', async () => {
    await tail(rows(1, 60), null);
    const middle = await readLocalStoreAround('ses_a', 'msg_030', 5);
    expect(num(middle?.messages)).toEqual(rows(25, 35).map((_, i) => 25 + i));
    expect([middle?.moreStored, middle?.startReached, middle?.reachesTail]).toEqual([true, false, false]);
    const nearTail = await readLocalStoreAround('ses_a', 'msg_058', 5);
    expect(num(nearTail?.messages)).toEqual(rows(53, 60).map((_, i) => 53 + i));
    expect(nearTail?.reachesTail).toBe(true);
    const nearStart = await readLocalStoreAround('ses_a', 'msg_002', 5);
    expect(nearStart?.startReached).toBe(true);
    // Once the stream broke, nothing says the stored newest row is the newest.
    noteLocalStoreStreamGap();
    expect((await readLocalStoreAround('ses_a', 'msg_058', 5))?.reachesTail).toBe(false);

    expect(await readLocalStoreAround('ses_a', 'msg_999', 5)).toBeNull();
    await tail(rows(1, 3, 'ses_b'), null, Date.now(), 'ses_b');
    expect(await readLocalStoreAround('ses_a', 'msg_ses_b_001', 5)).toBeNull();
    // A row the stream delivered while a read was out is stored, but no page
    // proved what lies around it.
    beginLocalStoreRead('ses_c');
    await recordLocalStoreStreamRow(row(5, {}, 'ses_c'));
    expect(num((await readLocalStoreWindow('ses_c'))?.messages)).toEqual([5]);
    expect(await readLocalStoreAround('ses_c', 'msg_ses_c_005', 5)).toBeNull();
  });
});

describe('P1: last observed wins, per row', () => {
  it('keeps a row streamed after a slower page started, for every page shape', async () => {
    for (const kind of ['tail', 'before', 'around'] as const) {
      // An authorization-change clear: the store stays claimed for this reader.
      await clearLocalSnapshots();
      // Read before the slow page went out, so only the streamed copy is newer.
      await tail(rows(1, 5), null, Date.now() - 3000);
      const slow = beginLocalStoreRead('ses_a', Date.now() - 2000);
      await recordLocalStoreStreamRow(row(3, { text: 'quick reply chosen' }));
      await recordLocalStorePage(slow, { kind }, { messages: rows(1, 5), next_before_id: null, next_after_id: null, anchor_id: 'msg_003' });
      expect(await texts()).toContain('quick reply chosen');
    }
  });

  it('does not let an older answer insert a row a newer page left out, though none was stored', async () => {
    const older = beginLocalStoreRead('ses_a', Date.now() - 2000);
    // Row 6 moved away; the newer complete tail lands first in an empty store.
    await tail(rows(1, 5), null);
    await recordLocalStorePage(older, { kind: 'tail' }, { messages: rows(1, 6), next_before_id: null });
    expect(await ids()).toEqual([1, 2, 3, 4, 5]);
  });

  it('does not let an older answer bring back a row a newer page left out, though an older removal left a tombstone', async () => {
    // Row 6 was removed at t1, then moved back into the session.
    await tail(rows(1, 6), null, Date.now() - 3000);
    await tail(rows(1, 5), null, Date.now() - 2500);
    // A read at t2 sees it back; a newer complete tail at t3, without it again,
    // lands first and finds it already gone.
    const older = beginLocalStoreRead('ses_a', Date.now() - 2000);
    await tail(rows(1, 5), null);
    await recordLocalStorePage(older, { kind: 'tail' }, { messages: rows(1, 6), next_before_id: null });
    expect(await ids()).toEqual([1, 2, 3, 4, 5]);
  });

  it('does not let an older answer move back a row a newer page left out, though it is stored under another session', async () => {
    // Row 6 is stored under the session it moved to.
    await tail([row(6, { session_id: 'ses_b' })], null, Date.now() - 3000, 'ses_b');
    // A read at t2 sees it back in this session; a newer complete tail at t3,
    // without it, lands first.
    const older = beginLocalStoreRead('ses_a', Date.now() - 2000);
    await tail(rows(1, 5), null);
    await recordLocalStorePage(older, { kind: 'tail' }, { messages: rows(1, 6), next_before_id: null });
    expect(await ids()).toEqual([1, 2, 3, 4, 5]);
    expect(await ids('ses_b')).toEqual([6]);
  });

  it('does not let a streamed row land after a newer page that left it out', async () => {
    await tail(rows(1, 5), null, Date.now() - 3000);
    // A tail whose request went out after row 6 arrived, without it, lands first.
    const arrival = Date.now();
    await tail(rows(1, 5), null, arrival + 1000);
    await recordLocalStoreStreamRow(row(6));
    expect(await ids()).toEqual([1, 2, 3, 4, 5]);
  });

  it('does not let an older answer bring back a row a newer one removed', async () => {
    const older = beginLocalStoreRead('ses_a', Date.now() - 2000);
    await tail(rows(1, 9), null, Date.now() - 1000);
    // The newer tail says row 9 is gone; the older one, landing last, still has it.
    await tail(rows(1, 8), null);
    await recordLocalStorePage(older, { kind: 'tail' }, { messages: rows(1, 9), next_before_id: null });
    expect(await ids()).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });
});

describe('the event stream', () => {
  it('stores a streamed row only for a session the store holds or this document is reading', async () => {
    await recordLocalStoreStreamRow(row(1, {}, 'ses_unknown'));
    expect(await ids('ses_unknown')).toEqual([]);

    beginLocalStoreRead('ses_opening');
    await recordLocalStoreStreamRow(row(1, {}, 'ses_opening'));
    expect(await ids('ses_opening')).toEqual([1]);

    // A read the session's forget voided admits nothing more.
    beginLocalStoreRead('ses_moved');
    applyLocalStoreSessionActivity({ session_id: 'ses_moved', event: 'updated', visibility: 'background' });
    await forgetLanded('ses_moved');
    // The row arrives after the forget.
    await pastForget('ses_moved');
    await recordLocalStoreStreamRow(row(1, {}, 'ses_moved'));
    expect(await ids('ses_moved')).toEqual([]);
  });

  it('covers a row streamed while a tail was out, past a later gap', async () => {
    const opening = beginLocalStoreRead('ses_a', Date.now() - 500);
    await recordLocalStoreStreamRow(row(6));
    await recordLocalStorePage(opening, { kind: 'tail' }, { messages: rows(1, 5), next_before_id: null }, session());
    // Row 6 came on the stream while the tail was out. It stays covered after
    // a later gap ends the tail claim, with no newer row to reach over it.
    noteLocalStoreStreamGap();
    const window = await readLocalStoreWindow('ses_a');
    expect(window?.messages.map((message) => message.id)).toEqual(rows(1, 6).map((message) => message.id));
    expect(window?.contiguous && window.startReached).toBe(true);
  });

  it('removes a stored row that moved to a session the store does not keep', async () => {
    await tail(rows(1, 3), null, Date.now() - 3000);
    const older = beginLocalStoreRead('ses_a', Date.now() - 2000);
    // promote_suppressed_native_message rebinds row 3; its message.new names the new session.
    await recordLocalStoreStreamRow({ ...row(3), session_id: 'ses_elsewhere' });
    expect(await ids()).toEqual([1, 2]);
    expect(await ids('ses_elsewhere')).toEqual([]);
    // A page read before the move, landing after it, does not bring it back.
    await recordLocalStorePage(older, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null });
    expect(await ids()).toEqual([1, 2]);
  });

  it('does not extend coverage over a row that arrived after the stream broke', async () => {
    await tail(rows(1, 5), null);
    noteLocalStoreStreamGap();
    await recordLocalStoreStreamRow(row(9));
    expect((await readLocalStoreWindow('ses_a'))?.contiguous).toBe(false);
  });
});

describe('one reader, and the wipes that end it', () => {
  it('LSYNC-008 shows a different reader on the same device nothing of the previous one', async () => {
    await tail(rows(1, 3), null);
    setLocalSnapshotPrincipal('remote:bob');
    expect(await ids()).toEqual([]);
    setLocalSnapshotPrincipal('remote:alice');
    expect(await ids()).toEqual([]);
  });

  it('LSYNC-007 wipes on sign-out, and a write still scheduled for the reader cannot put it back', async () => {
    await tail(rows(1, 3), null);
    const pending = beginLocalStoreRead('ses_a');
    await forgetLocalSnapshots();
    setLocalSnapshotPrincipal('remote:alice');
    await recordLocalStorePage(pending, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session());
    expect(await ids()).toEqual([]);
  });

  it('LSYNC-019 clears the store on an authorization change', async () => {
    await tail(rows(1, 3), null);
    await clearLocalSnapshots();
    expect(await ids()).toEqual([]);
  });

  it('LSYNC-018 wipes on a sign-out in another tab, including one running an older build', async () => {
    await tail(rows(1, 3), null);
    expect(await rowsOnDisk()).toBe(3);
    // An older build bumps the shared generation and knows nothing of this store.
    bumpSignOutGeneration();
    window.dispatchEvent(new StorageEvent('storage', { key: SIGN_OUT_GENERATION_KEY }));
    // Off the disk now, not only hidden until the next open.
    await vi.waitFor(async () => expect(await rowsOnDisk()).toBe(0), SETTLE);
  });

  it('LSYNC-018 stores and reads nothing more for a reader signed out in another tab', async () => {
    await tail(rows(1, 3), null);
    bumpSignOutGeneration();
    window.dispatchEvent(new StorageEvent('storage', { key: SIGN_OUT_GENERATION_KEY }));
    await vi.waitFor(async () => expect(await rowsOnDisk()).toBe(0), SETTLE);
    // The principal is still named in this tab; a cached answer still arrives.
    await tail(rows(1, 3), null);
    expect(await readLocalStoreWindow('ses_a')).toBeNull();
    setLocalSnapshotPrincipal(null);
    setLocalSnapshotPrincipal('remote:alice');
    expect(await ids()).toEqual([]);
  });

  it('LSYNC-018 refuses to read or write for the reader even when the sign-out event never reached this tab', async () => {
    await tail(rows(1, 3), null);
    // The generation moved (a sign-out elsewhere) and no storage event arrived.
    bumpSignOutGeneration();
    expect(await readLocalStoreWindow('ses_a')).toBeNull();
    await tail(rows(1, 5), null);
    expect(await rowsOnDisk()).toBe(3);
  });

  it('LSYNC-018 wipes at the next open after such a sign-out when no current tab was open', async () => {
    await tail(rows(1, 3), null);
    bumpSignOutGeneration();
    vi.resetModules();
    const fresh = await import('.');
    const snapshots = await import('../localSnapshots');
    snapshots.setLocalSnapshotPrincipal('remote:alice');
    expect((await fresh.readLocalStoreWindow('ses_a'))?.messages ?? []).toEqual([]);
    // Emptied by the claim, not only hidden.
    expect(await rowsOnDisk()).toBe(0);
    snapshots.setLocalSnapshotPrincipal(null);
  });

  it('LSYNC-021 wipes when any read reports the session expired, before login can take the page away', async () => {
    await tail(rows(1, 3), null);
    // The iOS Home Screen app hands login to its gate instead of navigating;
    // either way the hand-off comes after the wipe has committed.
    platform.ios = true;
    platform.standalone = true;
    const { REMOTE_AUTH_REQUIRED_EVENT, reportRemoteAuthorizationState } = await import('../remoteAuth');
    // At the hand-off the wipe has committed: nothing is left pending.
    let pendingAtHandOff: string | null | undefined;
    const onHandOff = () => {
      pendingAtHandOff = window.localStorage.getItem('avibe.local-store.wipe-pending.v1');
    };
    window.addEventListener(REMOTE_AUTH_REQUIRED_EVENT, onHandOff);
    try {
      reportRemoteAuthorizationState('login_required');
      await vi.waitFor(() => expect(pendingAtHandOff).not.toBeUndefined(), SETTLE);
      expect(pendingAtHandOff).toBeNull();
      expect(await rowsOnDisk()).toBe(0);
    } finally {
      window.removeEventListener(REMOTE_AUTH_REQUIRED_EVENT, onHandOff);
    }
  });

  it('LSYNC-021 finishes, at the next open, a wipe the page did not live to commit', async () => {
    await tail(rows(1, 3), null);
    // A wipe marked itself pending and the page unloaded before it committed.
    window.localStorage.setItem('avibe.local-store.wipe-pending.v1', 'interrupted');
    vi.resetModules();
    const fresh = await import('.');
    const snapshots = await import('../localSnapshots');
    snapshots.setLocalSnapshotPrincipal('remote:alice');
    await fresh.readLocalStoreWindow('ses_a');
    expect(await rowsOnDisk()).toBe(0);
    expect(window.localStorage.getItem('avibe.local-store.wipe-pending.v1')).toBeNull();
    snapshots.setLocalSnapshotPrincipal(null);
  });

  it('gives up a wipe that itself runs out of space, leaving it pending for the next open', async () => {
    await tail(rows(1, 3), null);
    // Every write of the store's metadata fails for space, the wipe's included.
    const put = IDBObjectStore.prototype.put;
    let metaWrites = 0;
    const full = vi.spyOn(IDBObjectStore.prototype, 'put').mockImplementation(function (this: IDBObjectStore, ...args) {
      if (this.name !== 'meta') return put.apply(this, args);
      metaWrites += 1;
      throw new DOMException('The quota has been exceeded.', 'QuotaExceededError');
    });
    let outcome: string;
    try {
      // A wipe that wiped again on its own failure would never settle.
      outcome = await Promise.race([
        wipeLocalStore().then(() => 'settled'),
        new Promise<string>((resolve) => setTimeout(resolve, 2_000, 'still wiping')),
      ]);
    } finally {
      full.mockRestore();
    }
    expect(outcome).toBe('settled');
    expect(metaWrites).toBe(1);
    expect(window.localStorage.getItem('avibe.local-store.wipe-pending.v1')).not.toBeNull();
  });

  it('LSYNC-020 keeps the forget fence when a fresh page lands after it', async () => {
    await tail(rows(1, 60), null);
    // An older-history read is out when the session is forgotten; a fresh tail
    // read after the forget lands first.
    const before = beginLocalStoreRead('ses_a', Date.now() - 1000);
    applyLocalStoreSessionActivity({ session_id: 'ses_a', event: 'updated', visibility: 'background' });
    await forgetLanded('ses_a');
    await pastForget('ses_a');
    await tail(rows(51, 60));
    await recordLocalStorePage(before, { kind: 'before' }, { messages: rows(1, 50), next_before_id: null });
    expect(await ids()).toEqual(rows(51, 60).map((message) => Number(message.id.slice(-3))));
    // Still kept as the session written again: a streamed row extends it.
    await recordLocalStoreStreamRow(row(61));
    expect((await ids()).at(-1)).toBe(61);
  });

  it('LSYNC-020 forgets a session that leaves the Inbox, and voids a read of it still in flight', async () => {
    await tail(rows(1, 3), null);
    await tail(rows(1, 2, 'ses_b'), null, Date.now(), 'ses_b');
    const pending = beginLocalStoreRead('ses_a');
    applyLocalStoreSessionActivity({ session_id: 'ses_a', event: 'archived' });
    applyLocalStoreSessionActivity({ session_id: 'ses_b', event: 'updated' });
    await forgetLanded('ses_a');
    await recordLocalStorePage(pending, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session());
    expect(await ids()).toEqual([]);
    expect(await ids('ses_b')).toEqual([1, 2]);
  });
});

describe('two tabs sharing the store', () => {
  // Another document of the same origin: its own module state, the same
  // IndexedDB and localStorage.
  const otherTab = async () => {
    vi.resetModules();
    const store = await import('.');
    const snapshots = await import('../localSnapshots');
    return { store, snapshots };
  };

  it('LSYNC-022 does not let a tab still naming an expired reader write into the store claimed for the next one', async () => {
    await tail(rows(1, 3), null);
    const pending = beginLocalStoreRead('ses_a');
    // Alice's session expires; Bob signs in in another tab.
    await wipeLocalStore();
    const bob = await otherTab();
    bob.snapshots.setLocalSnapshotPrincipal('remote:bob');
    await bob.store.readLocalStoreWindow('ses_a');

    await recordLocalStorePage(pending, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session());
    await tail(rows(4, 6), null);
    expect(await rowsOnDisk()).toBe(0);
    expect((await bob.store.readLocalStoreWindow('ses_a'))?.messages ?? []).toEqual([]);
    bob.snapshots.setLocalSnapshotPrincipal(null);
  });

  it('LSYNC-022 does not let an authorization-change clear in a stale tab claim the store another reader holds', async () => {
    const bob = await otherTab();
    bob.snapshots.setLocalSnapshotPrincipal('remote:bob');
    await bob.store.readLocalStoreWindow('ses_b');
    // Alice's tab, still naming Alice, clears on an authorization change.
    await clearLocalSnapshots();
    await tail(rows(1, 3), null);
    expect(await rowsOnDisk()).toBe(0);
    bob.snapshots.setLocalSnapshotPrincipal(null);
  });

  it('drops a write whose transaction lands after another tab cleared the store for the same reader', async () => {
    await tail(rows(1, 3), null);
    const other = await otherTab();
    other.snapshots.setLocalSnapshotPrincipal('remote:alice');
    await other.store.readLocalStoreWindow('ses_a');
    // This tab's write is under way when the other tab clears on an
    // authorization change; the store stays Alice's at a new epoch.
    const writing = tail(rows(4, 6), null);
    const clearing = other.snapshots.clearLocalSnapshots();
    await Promise.all([writing, clearing]);
    expect(await rowsOnDisk()).toBe(0);
    other.snapshots.setLocalSnapshotPrincipal(null);
  });

  it('keeps writing after the epoch mirror in localStorage was cleared on its own', async () => {
    await tail(rows(1, 3), null);
    window.localStorage.clear();
    // A new document names the same reader; the store and its meta are intact.
    const next = await otherTab();
    next.snapshots.setLocalSnapshotPrincipal('remote:alice');
    await next.store.recordLocalStorePage(
      next.store.beginLocalStoreRead('ses_a'),
      { kind: 'tail' },
      { messages: rows(1, 4), next_before_id: null },
    );
    await next.store.recordLocalStorePage(
      next.store.beginLocalStoreRead('ses_a'),
      { kind: 'tail' },
      { messages: rows(1, 5), next_before_id: null },
    );
    expect(((await next.store.readLocalStoreWindow('ses_a'))?.messages ?? []).length).toBe(5);
    next.snapshots.setLocalSnapshotPrincipal(null);
  });

  it('LSYNC-020 does not let a read pending in one tab admit streamed rows of a session another tab forgot', async () => {
    beginLocalStoreRead('ses_p');
    await recordLocalStoreStreamRow(row(1, {}, 'ses_p'));
    const other = await otherTab();
    other.snapshots.setLocalSnapshotPrincipal('remote:alice');
    await other.store.readLocalStoreWindow('ses_p');
    other.store.applyLocalStoreSessionActivity({ session_id: 'ses_p', event: 'archived' });
    await forgetLanded('ses_p');
    // The row arrives after the forget: only the read pending from before it could admit it.
    await pastForget('ses_p');
    await recordLocalStoreStreamRow(row(2, {}, 'ses_p'));
    expect(await ids('ses_p')).toEqual([]);
    other.snapshots.setLocalSnapshotPrincipal(null);
  });

  it('LSYNC-020 does not let a row that arrived before a forget land after it', async () => {
    await tail(rows(1, 3), null);
    // Another tab: its row arrives while its own claim is still landing, so its
    // write lands after this tab's forget. A read it starts after the forget is
    // pending, yet the row itself came before the forget.
    const other = await otherTab();
    other.snapshots.setLocalSnapshotPrincipal('remote:alice');
    const streaming = other.store.recordLocalStoreStreamRow(row(9));
    const forgetting = forgetLocalStoreSession('ses_a');
    other.store.beginLocalStoreRead('ses_a', Date.now() + 60_000);
    await Promise.all([streaming, forgetting]);
    expect(await rowsOnDisk()).toBe(0);
    other.snapshots.setLocalSnapshotPrincipal(null);
  });

  it('LSYNC-020 does not let a read in one tab restore a session another tab forgot', async () => {
    await tail(rows(1, 3), null);
    const pending = beginLocalStoreRead('ses_a');
    const other = await otherTab();
    other.snapshots.setLocalSnapshotPrincipal('remote:alice');
    await other.store.readLocalStoreWindow('ses_a');
    other.store.applyLocalStoreSessionActivity({ session_id: 'ses_a', event: 'archived' });
    await forgetLanded('ses_a');

    await recordLocalStorePage(pending, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session());
    expect(await ids()).toEqual([]);
    other.snapshots.setLocalSnapshotPrincipal(null);
  });
});

describe('session rows', () => {
  it('keeps a stored session\'s pin and title in step with session updates', async () => {
    await tail(rows(1, 2), null);
    applyLocalStoreSessionActivity({ session_id: 'ses_a', event: 'updated', pinned: true, title: 'renamed' });
    await vi.waitFor(async () => expect((await readLocalStoreWindow('ses_a'))?.session?.pinned).toBe(true), SETTLE);
    const stored = (await readLocalStoreWindow('ses_a'))?.session;
    expect(stored?.pinned).toBe(true);
    expect(stored?.title).toBe('renamed');
  });
});

describe('attachment metadata', () => {
  // A card's name, type, and size (decision D1: never the file), kept with the
  // session it was shown in, so the card shows offline.
  const url = '/api/media/tok_report';
  const card = { name: 'report.pdf', ext: 'pdf', content_type: 'application/pdf', size: 2048 };

  it('keeps the metadata of an attachment shown in a held session, through later session reads', async () => {
    await tail(rows(1, 3), null);
    await recordLocalStoreMediaMeta(beginLocalStoreRead('ses_a'), url, card);
    // A later session read rewrites the session row; the attachment stays.
    await tail(rows(1, 3), null);
    expect(await readLocalStoreMediaMeta('ses_a', url)).toEqual(card);
    // A session the store does not keep keeps none.
    await recordLocalStoreMediaMeta(beginLocalStoreRead('ses_x'), url, card);
    expect(await readLocalStoreMediaMeta('ses_x', url)).toBeNull();
  });

  it('LSYNC-020 drops it with a forgotten session, and an answer read before the forget does not bring it back', async () => {
    await tail(rows(1, 3), null);
    await recordLocalStoreMediaMeta(beginLocalStoreRead('ses_a'), url, card);
    const late = beginLocalStoreRead('ses_a', Date.now() - 1000);
    await forgetLocalStoreSession('ses_a');
    expect(await readLocalStoreMediaMeta('ses_a', url)).toBeNull();
    // The session is read again after the forget; the answer from before it lands after.
    const after = Date.now() + 1000;
    await tail(rows(1, 3), null, after);
    await recordLocalStoreMediaMeta(late, url, card);
    expect(await readLocalStoreMediaMeta('ses_a', url)).toBeNull();
    // One read after the forget is kept.
    await recordLocalStoreMediaMeta(beginLocalStoreRead('ses_a', after), url, card);
    expect(await readLocalStoreMediaMeta('ses_a', url)).toEqual(card);
  });
});
