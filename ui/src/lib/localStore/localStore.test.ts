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
  readLocalStoreWindow,
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
const idle = () => new Promise((resolve) => setTimeout(resolve, 30));
// Eviction and hand-offs run on their own; under a full parallel suite they can
// take well over vitest's one-second default to land.
const SETTLE = { timeout: 10_000, interval: 25 };
/** Give the page a ``navigator.storage``. Defined on the real navigator rather
 *  than by replacing it: fake-indexeddb finds its task scheduler through
 *  ``navigator.userAgent``, and without it falls back to setTimeout, which a
 *  case with fake timers then swallows. */
const stubStorage = (storage: Partial<StorageManager>) =>
  Object.defineProperty(navigator, 'storage', { configurable: true, value: storage });
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
  Reflect.deleteProperty(navigator, 'storage');
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

describe('P1: last observed wins, per row', () => {
  it('keeps a row streamed after a slower page started, for every page shape', async () => {
    for (const kind of ['tail', 'before', 'around'] as const) {
      // An authorization-change clear: the store stays claimed for this reader.
      await clearLocalSnapshots();
      await tail(rows(1, 5), null);
      const slow = beginLocalStoreRead('ses_a', Date.now() - 1000);
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
    await idle();
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
    await tail(rows(1, 3), null);
    // promote_suppressed_native_message rebinds row 3; its message.new names the new session.
    await recordLocalStoreStreamRow({ ...row(3), session_id: 'ses_elsewhere' });
    expect(await ids()).toEqual([1, 2]);
    expect(await ids('ses_elsewhere')).toEqual([]);
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
    await idle();
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
    await idle();
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
    await idle();
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
    await idle();
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
    await idle();

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

describe('budget and persistence', () => {
  // A fresh document runs its first budget check on a zero-delay timer after its
  // first write. Writing every page at once queues all their transactions
  // before that check creates its own, and IndexedDB runs them in that order,
  // so the check sees every row, with no fake timers and no race with load.
  const freshDocument = async () => {
    vi.resetModules();
    const store = await import('.');
    const snapshots = await import('../localSnapshots');
    snapshots.setLocalSnapshotPrincipal('remote:alice');
    const held = async (sessionId: string) => ((await store.readLocalStoreWindow(sessionId))?.messages ?? []).length;
    const done = async () => {
      await store.wipeLocalStore();
      snapshots.setLocalSnapshotPrincipal(null);
    };
    return { store, held, done };
  };
  const big = (n: number, sessionId: string) => row(n, { text: 'x'.repeat(20_000) }, sessionId);

  it('evicts the least recently active session first and keeps the main Bot', async () => {
    // A budget (half the quota) between two and three sessions' rows.
    stubStorage({ estimate: async () => ({ quota: 200_000, usage: 0 }) });
    const { store, held, done } = await freshDocument();
    const page = (sessionId: string, lastActive: number) =>
      store.recordLocalStorePage(
        store.beginLocalStoreRead(sessionId),
        { kind: 'tail' },
        { messages: [big(1, sessionId), big(2, sessionId)], next_before_id: null },
        session(sessionId, { last_active_at: at(lastActive) }),
      );
    const readBeforeEviction = store.beginLocalStoreRead('ses_old', Date.now() - 1000);
    await Promise.all([page('ses_main', 1), page('ses_old', 2), page('ses_new', 3)]);
    await vi.waitFor(async () => expect(await held('ses_old')).toBe(0), SETTLE);
    expect(await held('ses_main')).toBe(2);
    expect(await held('ses_new')).toBe(2);
    // An answer read before the eviction does not put the evicted rows back.
    await store.recordLocalStorePage(
      readBeforeEviction,
      { kind: 'tail' },
      { messages: [big(1, 'ses_old'), big(2, 'ses_old')], next_before_id: null },
    );
    expect(await held('ses_old')).toBe(0);
    await done();
  });

  it('counts rows of a session no session read has named yet against the budget', async () => {
    // A budget (half the quota) under two sessions' rows.
    stubStorage({ estimate: async () => ({ quota: 140_000, usage: 0 }) });
    const { store, held, done } = await freshDocument();
    await Promise.all([
      store.recordLocalStorePage(
        store.beginLocalStoreRead('ses_main'),
        { kind: 'tail' },
        { messages: [big(1, 'ses_main'), big(2, 'ses_main')], next_before_id: null },
        session('ses_main'),
      ),
      // An older page with no session row beside it.
      store.recordLocalStorePage(
        store.beginLocalStoreRead('ses_orphan'),
        { kind: 'before' },
        { messages: [big(1, 'ses_orphan'), big(2, 'ses_orphan')], next_before_id: 'cursor' },
      ),
    ]);
    await vi.waitFor(async () => expect(await held('ses_orphan')).toBe(0), SETTLE);
    expect(await held('ses_main')).toBe(2);
    await done();
  });

  it('asks for persistent storage silently in an installed app and not in a browser tab', async () => {
    const persist = vi.fn(async () => true);
    stubStorage({ persist, persisted: async () => false });
    const openAs = async (standalone: boolean) => {
      platform.standalone = standalone;
      vi.resetModules();
      const fresh = await import('.');
      const snapshots = await import('../localSnapshots');
      snapshots.setLocalSnapshotPrincipal('remote:alice');
      await fresh.readLocalStoreWindow('ses_a');
      await idle();
      snapshots.setLocalSnapshotPrincipal(null);
    };
    await openAs(false);
    expect(persist).not.toHaveBeenCalled();
    await openAs(true);
    expect(persist).toHaveBeenCalledTimes(1);
  });
});
