// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const platform = vi.hoisted(() => ({ standalone: false }));
vi.mock('../platform', () => ({ isStandalonePwa: () => platform.standalone }));

import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { clearLocalSnapshots, forgetLocalSnapshots, setLocalSnapshotPrincipal } from '../localSnapshots';
import { SIGN_OUT_GENERATION_KEY, bumpSignOutGeneration } from '../signOutGeneration';
import {
  beginLocalStoreRead,
  forgetLocalStoreSessionOnActivity,
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
      await wipeLocalStore();
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
    forgetLocalStoreSessionOnActivity({ session_id: 'ses_moved', event: 'updated', visibility: 'background' });
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
    await idle();
    // Off the disk now, not only hidden until the next open.
    expect(await rowsOnDisk()).toBe(0);
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

  it('LSYNC-018 wipes at the next open after such a sign-out when no current tab was open', async () => {
    await tail(rows(1, 3), null);
    bumpSignOutGeneration();
    vi.resetModules();
    const fresh = await import('.');
    const snapshots = await import('../localSnapshots');
    snapshots.setLocalSnapshotPrincipal('remote:alice');
    expect((await fresh.readLocalStoreWindow('ses_a'))?.messages ?? []).toEqual([]);
    snapshots.setLocalSnapshotPrincipal(null);
  });

  it('LSYNC-020 forgets a session that leaves the Inbox, and voids a read of it still in flight', async () => {
    await tail(rows(1, 3), null);
    await tail(rows(1, 2, 'ses_b'), null, Date.now(), 'ses_b');
    const pending = beginLocalStoreRead('ses_a');
    forgetLocalStoreSessionOnActivity({ session_id: 'ses_a', event: 'archived' });
    forgetLocalStoreSessionOnActivity({ session_id: 'ses_b', event: 'updated' });
    await idle();
    await recordLocalStorePage(pending, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session());
    expect(await ids()).toEqual([]);
    expect(await ids('ses_b')).toEqual([1, 2]);
  });
});

describe('budget and persistence', () => {
  it('evicts the least recently active session first and keeps the main Bot', async () => {
    // A budget (half the quota) between two and three sessions' rows.
    vi.stubGlobal('navigator', { ...navigator, storage: { estimate: async () => ({ quota: 200_000, usage: 0 }) } });
    // The budget check is throttled on a timer, so this case needs a document
    // with no check pending. IndexedDB here runs on setImmediate, not timers.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    vi.resetModules();
    const fresh = await import('.');
    const snapshots = await import('../localSnapshots');
    snapshots.setLocalSnapshotPrincipal('remote:alice');
    const big = (n: number, sessionId: string) => row(n, { text: 'x'.repeat(20_000) }, sessionId);
    const page = (sessionId: string, lastActive: number) =>
      fresh.recordLocalStorePage(
        fresh.beginLocalStoreRead(sessionId),
        { kind: 'tail' },
        { messages: [big(1, sessionId), big(2, sessionId)], next_before_id: null },
        session(sessionId, { last_active_at: at(lastActive) }),
      );
    const held = async (sessionId: string) =>
      ((await fresh.readLocalStoreWindow(sessionId))?.messages ?? []).length;
    try {
      await page('ses_main', 1);
      await page('ses_old', 2);
      await page('ses_new', 3);
      await vi.advanceTimersByTimeAsync(31_000);
    } finally {
      vi.useRealTimers();
    }
    await vi.waitFor(async () => expect(await held('ses_old')).toBe(0));
    expect(await held('ses_main')).toBe(2);
    expect(await held('ses_new')).toBe(2);
    await fresh.wipeLocalStore();
    snapshots.setLocalSnapshotPrincipal(null);
  });

  it('counts rows of a session no session read has named yet against the budget', async () => {
    // A budget (half the quota) under two sessions' rows.
    vi.stubGlobal('navigator', { ...navigator, storage: { estimate: async () => ({ quota: 140_000, usage: 0 }) } });
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    vi.resetModules();
    const fresh = await import('.');
    const snapshots = await import('../localSnapshots');
    snapshots.setLocalSnapshotPrincipal('remote:alice');
    const big = (n: number, sessionId: string) => row(n, { text: 'x'.repeat(20_000) }, sessionId);
    const held = async (sessionId: string) =>
      ((await fresh.readLocalStoreWindow(sessionId))?.messages ?? []).length;
    try {
      await fresh.recordLocalStorePage(
        fresh.beginLocalStoreRead('ses_main'),
        { kind: 'tail' },
        { messages: [big(1, 'ses_main'), big(2, 'ses_main')], next_before_id: null },
        session('ses_main'),
      );
      // An older page with no session row beside it.
      await fresh.recordLocalStorePage(
        fresh.beginLocalStoreRead('ses_orphan'),
        { kind: 'before' },
        { messages: [big(1, 'ses_orphan'), big(2, 'ses_orphan')], next_before_id: 'cursor' },
      );
      await vi.advanceTimersByTimeAsync(31_000);
    } finally {
      vi.useRealTimers();
    }
    await vi.waitFor(async () => expect(await held('ses_orphan')).toBe(0));
    expect(await held('ses_main')).toBe(2);
    await fresh.wipeLocalStore();
    snapshots.setLocalSnapshotPrincipal(null);
  });

  it('asks for persistent storage silently in an installed app and not in a browser tab', async () => {
    const persist = vi.fn(async () => true);
    vi.stubGlobal('navigator', { ...navigator, storage: { persist, persisted: async () => false } });
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
