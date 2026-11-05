// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

const platform = vi.hoisted(() => ({ standalone: false }));
vi.mock('../platform', () => ({ isStandalonePwa: () => platform.standalone, isIosDevice: () => false }));

import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';

// The local message store's budget (decision D2) and persistence (D5), in a file
// of their own: the budget is a sum over the whole database, so these cases get
// a database no other case's documents still write into.
//
// Each document is a fresh module instance sharing only IndexedDB and
// localStorage, as tabs do. A document runs its first budget check right after
// its first write and later ones at most every 30 seconds, so a case writes in
// one document and lets another one's first write run the check over everything
// stored by then.

const at = (n: number) => `2026-11-05T00:00:${String(n).padStart(2, '0')}.000000Z`;
const row = (n: number, sessionId: string, text = 'x'.repeat(20_000)): WorkbenchMessage => ({
  id: `msg_${sessionId}_${String(n).padStart(3, '0')}`,
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
  text,
  content: {},
  metadata: {},
  created_at: at(n),
  updated_at: at(n),
  delivered_at: null,
  read_at: null,
});
const session = (id: string, lastActive: number) =>
  ({ id, title: id, status: 'active', pinned: false, last_active_at: at(lastActive), updated_at: at(lastActive) }) as WorkbenchSession;
const SETTLE = { timeout: 10_000, interval: 25 };
/** Give the page a ``navigator.storage``, on the real navigator: a replaced one
 *  would lose ``userAgent``, which fake-indexeddb picks its scheduler by. */
const stubStorage = (storage: Partial<StorageManager>) =>
  Object.defineProperty(navigator, 'storage', { configurable: true, value: storage });

/** Everything on disk, read past every fence, for a failure to explain itself. */
const onDisk = () => new Promise<string>((resolve) => {
  const open = indexedDB.open('avibe-local-store');
  open.onsuccess = () => {
    const db = open.result;
    const tx = db.transaction(['meta', 'messages', 'sessions'], 'readonly');
    const meta = tx.objectStore('meta').getAll();
    const messages = tx.objectStore('messages').getAll();
    const sessions = tx.objectStore('sessions').getAll();
    tx.oncomplete = () => {
      db.close();
      const rows: Record<string, number> = {};
      for (const message of messages.result as { sessionId: string }[]) rows[message.sessionId] = (rows[message.sessionId] ?? 0) + 1;
      resolve(JSON.stringify({
        meta: meta.result,
        rows,
        sessions: (sessions.result as { id: string; bytes: number; lastActiveAt: string }[]).map((s) => [s.id, s.bytes, s.lastActiveAt]),
      }));
    };
  };
});

const documents: { wipe: () => Promise<void>; leave: () => void }[] = [];

const openDocument = async () => {
  vi.resetModules();
  const store = await import('.');
  const snapshots = await import('../localSnapshots');
  snapshots.setLocalSnapshotPrincipal('remote:alice');
  documents.push({ wipe: store.wipeLocalStore, leave: () => snapshots.setLocalSnapshotPrincipal(null) });
  const held = async (sessionId: string) => ((await store.readLocalStoreWindow(sessionId))?.messages ?? []).length;
  const tail = (sessionId: string, messages: WorkbenchMessage[], row?: WorkbenchSession) =>
    store.recordLocalStorePage(store.beginLocalStoreRead(sessionId), { kind: 'tail' }, { messages, next_before_id: null }, row);
  return { store, held, tail };
};

/** Run a budget check over everything stored so far. */
const checkBudget = async () => {
  const next = await openDocument();
  await next.tail('ses_check', [row(1, 'ses_check', 'check')], session('ses_check', 59));
  return next;
};

afterEach(async () => {
  for (const document of documents.splice(0)) document.leave();
  // Whatever the case left, the next one starts from an empty store.
  const last = await openDocument();
  await last.store.wipeLocalStore();
  for (const document of documents.splice(0)) document.leave();
  platform.standalone = false;
  Reflect.deleteProperty(navigator, 'storage');
  window.localStorage.clear();
});

describe('budget', () => {
  it('evicts the least recently active session first and keeps the main Bot', async () => {
    // A budget (half the quota) between two and three sessions' rows.
    stubStorage({ estimate: async () => ({ quota: 200_000, usage: 0 }) });
    const tab = await openDocument();
    const readBeforeEviction = tab.store.beginLocalStoreRead('ses_old', Date.now() - 1000);
    await tab.tail('ses_main', [row(1, 'ses_main'), row(2, 'ses_main')], session('ses_main', 1));
    await tab.tail('ses_old', [row(1, 'ses_old'), row(2, 'ses_old')], session('ses_old', 2));
    await tab.tail('ses_new', [row(1, 'ses_new'), row(2, 'ses_new')], session('ses_new', 3));
    await checkBudget();
    await vi.waitFor(async () => expect(await tab.held('ses_old'), await onDisk()).toBe(0), SETTLE);
    expect(await tab.held('ses_main'), await onDisk()).toBe(2);
    expect(await tab.held('ses_new'), await onDisk()).toBe(2);
    // An answer read before the eviction does not put the evicted rows back.
    await tab.store.recordLocalStorePage(
      readBeforeEviction,
      { kind: 'tail' },
      { messages: [row(1, 'ses_old'), row(2, 'ses_old')], next_before_id: null },
    );
    expect(await tab.held('ses_old'), await onDisk()).toBe(0);
  });

  it('counts rows of a session no session read has named yet against the budget', async () => {
    // A budget (half the quota) under two sessions' rows.
    stubStorage({ estimate: async () => ({ quota: 140_000, usage: 0 }) });
    const tab = await openDocument();
    await tab.tail('ses_main', [row(1, 'ses_main'), row(2, 'ses_main')], session('ses_main', 1));
    // An older page with no session row beside it.
    await tab.store.recordLocalStorePage(
      tab.store.beginLocalStoreRead('ses_orphan'),
      { kind: 'before' },
      { messages: [row(1, 'ses_orphan'), row(2, 'ses_orphan')], next_before_id: 'cursor' },
    );
    await checkBudget();
    await vi.waitFor(async () => expect(await tab.held('ses_orphan'), await onDisk()).toBe(0), SETTLE);
    expect(await tab.held('ses_main'), await onDisk()).toBe(2);
  });

  it('ranks a conversation by the messages that arrive on the stream, not only by its last session read', async () => {
    // A budget (half the quota) between two and three sessions' rows.
    stubStorage({ estimate: async () => ({ quota: 200_000, usage: 0 }) });
    const tab = await openDocument();
    await tab.tail('ses_live', [row(1, 'ses_live'), row(2, 'ses_live')], session('ses_live', 2));
    // A session read goes out before the conversation's newest message arrives,
    // and its answer lands after it, still naming the old activity.
    const sessionRead = tab.store.beginLocalStoreRead('ses_live');
    await tab.store.recordLocalStoreStreamRow(row(9, 'ses_live', 'newest'));
    await tab.store.recordLocalStoreSession(sessionRead, session('ses_live', 2));
    await tab.tail('ses_a', [row(1, 'ses_a'), row(2, 'ses_a')], session('ses_a', 5));
    await tab.tail('ses_b', [row(1, 'ses_b'), row(2, 'ses_b')], session('ses_b', 4));
    await checkBudget();
    await vi.waitFor(async () => expect(await tab.held('ses_b'), await onDisk()).toBe(0), SETTLE);
    expect(await tab.held('ses_live'), await onDisk()).toBe(3);
    expect(await tab.held('ses_a'), await onDisk()).toBe(2);
  });
});

describe('persistence', () => {
  it('asks for persistent storage silently in an installed app and not in a browser tab', async () => {
    const persist = vi.fn(async () => true);
    stubStorage({ persist, persisted: async () => false });
    const openAs = async (standalone: boolean) => {
      platform.standalone = standalone;
      const tab = await openDocument();
      await tab.store.readLocalStoreWindow('ses_a');
    };
    await openAs(false);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(persist).not.toHaveBeenCalled();
    await openAs(true);
    await vi.waitFor(() => expect(persist).toHaveBeenCalledTimes(1), SETTLE);
  });
});
