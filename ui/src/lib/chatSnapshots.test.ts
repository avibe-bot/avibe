// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchMessage, WorkbenchSession } from '../context/ApiContext';
import { knownChatSnapshot, readChatSnapshot } from './chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from './localSnapshots';
import {
  beginLocalStoreRead,
  forgetLocalStoreSession,
  noteLocalStoreStreamGap,
  noteLocalStoreStreamState,
  recordLocalStorePage,
  recordLocalStoreSession,
  type LocalStoreChatChrome,
} from './localStore';

// The conversation a chat paints before its bootstrap answers, from the local
// message store (docs/plans/local-sync-store.md section 4.3): only once a
// bootstrap of it was stored, only its newest rows with no gap among them, and
// held in memory (the hot set) for the reader whose it is, so a chat opened
// again paints in its first frame.

const at = (n: number) => `2026-11-05T00:${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}.000000Z`;
const row = (n: number, sessionId = 'ses_a'): WorkbenchMessage => ({
  id: `msg_${sessionId}_${String(n).padStart(3, '0')}`,
  session_id: sessionId,
  type: 'result',
  author: 'agent',
  text: `row ${n}`,
  content: {},
  metadata: {},
  created_at: at(n),
  updated_at: at(n),
} as WorkbenchMessage);
const rows = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => row(from + i));
const session = { id: 'ses_a', title: 'A', status: 'active', last_active_at: at(1) } as WorkbenchSession;
const chrome: LocalStoreChatChrome = {
  agents: [{ id: 'agent-vibey', name: 'vibey' }, { broken: true }],
  default_agent_name: 'vibey',
  can_chat: true,
  chat_message_font_size: 16,
};
const ids = (messages: WorkbenchMessage[] | undefined) => (messages ?? []).map((message) => Number(message.id.slice(-3)));

// A bootstrap answer as the API layer stores it.
const bootstrap = (messages: WorkbenchMessage[], nextBeforeId: string | null = null, observedAt = Date.now()) =>
  recordLocalStorePage(beginLocalStoreRead('ses_a', observedAt), { kind: 'tail' }, { messages, next_before_id: nextBeforeId }, session, chrome);
// A page read without the bootstrap's chrome (a tail read, a recovery read).
const page = (messages: WorkbenchMessage[], nextBeforeId: string | null = null) =>
  recordLocalStorePage(beginLocalStoreRead('ses_a'), { kind: 'tail' }, { messages, next_before_id: nextBeforeId }, session);
const SETTLE = { timeout: 10_000, interval: 25 };
const eventually = (assertion: () => void | Promise<void>) => vi.waitFor(assertion, SETTLE);

beforeEach(() => {
  setLocalSnapshotPrincipal('remote:alice');
  noteLocalStoreStreamState(true);
});

afterEach(async () => {
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
  window.localStorage.clear();
});

describe('the conversation a chat paints', () => {
  it('is there once a bootstrap of it was stored, with what the bootstrap said around it', async () => {
    await page(rows(1, 3));
    expect(await readChatSnapshot('ses_a')).toBeNull();

    await bootstrap(rows(1, 3));
    const snapshot = await readChatSnapshot('ses_a');
    expect(snapshot?.session.title).toBe('A');
    expect(ids(snapshot?.messages)).toEqual([1, 2, 3]);
    expect(snapshot?.next_before_id).toBeNull();
    // An Agent the page could not render is left out.
    expect(snapshot?.agents.map((agent) => agent.name)).toEqual(['vibey']);
    expect([snapshot?.default_agent_name, snapshot?.can_chat, snapshot?.chat_message_font_size]).toEqual(['vibey', true, 16]);
  });

  it('keeps the newest rows a bootstrap returns, and the cursor the server would give for the older ones', async () => {
    await bootstrap(rows(1, 60));
    const whole = await readChatSnapshot('ses_a');
    expect(ids(whole?.messages)).toEqual(rows(11, 60).map((_, i) => 11 + i));
    expect(whole?.next_before_id).toBe(row(11).id);

    // A tail that does not reach the start of history pages older than its first row.
    await clearLocalSnapshots();
    await bootstrap(rows(51, 60), row(51).id);
    const tail = await readChatSnapshot('ses_a');
    expect(ids(tail?.messages)).toEqual(rows(51, 60).map((_, i) => 51 + i));
    expect(tail?.next_before_id).toBe(row(51).id);
  });

  it("keeps a bootstrap's chrome when a session read that went out after it answers first, and the newer row", async () => {
    const start = Date.now();
    const bootstrapRead = beginLocalStoreRead('ses_a', start);
    // The chat refreshes its row while its bootstrap is out; that read answers first.
    await recordLocalStoreSession(beginLocalStoreRead('ses_a', start + 10), { ...session, title: 'renamed' });
    await recordLocalStorePage(bootstrapRead, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session, chrome);
    const snapshot = await readChatSnapshot('ses_a');
    expect(snapshot?.session.title).toBe('renamed');
    expect(snapshot?.default_agent_name).toBe('vibey');
    expect(ids(snapshot?.messages)).toEqual([1, 2, 3]);

    // An older bootstrap landing later does not replace the newer one's chrome.
    const older = beginLocalStoreRead('ses_a', start - 10);
    await recordLocalStorePage(older, { kind: 'tail' }, { messages: rows(1, 3), next_before_id: null }, session, { ...chrome, default_agent_name: 'stale' });
    expect((await readChatSnapshot('ses_a'))?.default_agent_name).toBe('vibey');
  });

  it('is not painted across a gap the store cannot account for', async () => {
    await bootstrap(rows(1, 5));
    // The stream broke; a later tail does not reach back to row 5.
    noteLocalStoreStreamGap();
    await bootstrap(rows(9, 10), row(9).id);
    expect(await readChatSnapshot('ses_a')).toBeNull();
  });
});

describe('the hot set', () => {
  it('holds a conversation once it is stored, and again at the next open of the store for the same reader', async () => {
    await bootstrap(rows(1, 3));
    await eventually(() => expect(ids(knownChatSnapshot('ses_a')?.messages)).toEqual([1, 2, 3]));

    // A later load of the page: the reader is named again, and the store refills memory.
    setLocalSnapshotPrincipal(null);
    expect(knownChatSnapshot('ses_a')).toBeNull();
    setLocalSnapshotPrincipal('remote:alice');
    await eventually(() => expect(ids(knownChatSnapshot('ses_a')?.messages)).toEqual([1, 2, 3]));
  });

  it('follows every write: a newer row is in memory as soon as it is stored', async () => {
    await bootstrap(rows(1, 3));
    await eventually(() => expect(ids(knownChatSnapshot('ses_a')?.messages)).toEqual([1, 2, 3]));
    await page(rows(1, 4));
    await eventually(() => expect(ids(knownChatSnapshot('ses_a')?.messages)).toEqual([1, 2, 3, 4]));
  });

  it('LSYNC-019 is emptied at once when the store is cleared, and LSYNC-008 when another reader is named', async () => {
    await bootstrap(rows(1, 3));
    await eventually(() => expect(knownChatSnapshot('ses_a')).not.toBeNull());
    const clearing = clearLocalSnapshots();
    expect(knownChatSnapshot('ses_a')).toBeNull();
    await clearing;

    await bootstrap(rows(1, 3));
    await eventually(() => expect(knownChatSnapshot('ses_a')).not.toBeNull());
    setLocalSnapshotPrincipal('remote:bob');
    expect(knownChatSnapshot('ses_a')).toBeNull();
  });

  it('LSYNC-020 drops a forgotten conversation at once, and a read already out does not put it back', async () => {
    await bootstrap(rows(1, 3));
    await eventually(() => expect(knownChatSnapshot('ses_a')).not.toBeNull());
    const reading = readChatSnapshot('ses_a');
    // Let the read reach IndexedDB first, so it reads the conversation before the
    // forget removes it (requests only complete on a later task).
    for (let tick = 0; tick < 20; tick += 1) await Promise.resolve();
    const forgetting = forgetLocalStoreSession('ses_a');
    expect(knownChatSnapshot('ses_a')).toBeNull();
    const [read] = await Promise.all([reading, forgetting]);
    // Neither held nor handed to the chat that asked.
    expect(read).toBeNull();
    expect(knownChatSnapshot('ses_a')).toBeNull();
    expect(await readChatSnapshot('ses_a')).toBeNull();
  });
});
