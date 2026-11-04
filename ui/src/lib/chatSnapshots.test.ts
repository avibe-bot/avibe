import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchMessage, WorkbenchSession } from '../context/ApiContext';
import {
  CHAT_SNAPSHOT_ROWS,
  CHAT_SNAPSHOT_SCHEMA,
  CHAT_SNAPSHOT_SESSIONS,
  chatSnapshotKey,
  chatSnapshotOwner,
  chatSnapshotWindow,
  forgetChatSnapshot,
  forgetChatSnapshotOnActivity,
  readChatSnapshot,
  writeChatSnapshot,
  type ChatSnapshot,
} from './chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal, writeLocalSnapshot } from './localSnapshots';

// A stored conversation is shown before the server answers, so what it may hold
// and when it may be written decide what a reader can see that the server did
// not just say: rows of a send still pending, rows of another build, or a write
// that lands after the store was voided.

const row = (id: string, extra: Partial<WorkbenchMessage> = {}): WorkbenchMessage => ({
  id,
  scope_id: null,
  session_id: 'ses_a',
  platform: 'avibe',
  author: 'agent',
  type: 'result',
  source: 'agent',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: `text of ${id}`,
  content: {},
  metadata: {},
  created_at: '2026-11-01T08:00:00Z',
  updated_at: '2026-11-01T08:00:00Z',
  delivered_at: '2026-11-01T08:00:00Z',
  read_at: null,
  ...extra,
} as WorkbenchMessage);

const snapshot = (sessionId: string, rows: WorkbenchMessage[] = [row('m1')]): ChatSnapshot => ({
  session: { id: sessionId, title: `title of ${sessionId}` } as WorkbenchSession,
  messages: rows,
  next_before_id: null,
  agents: [],
  default_agent_name: null,
  can_chat: true,
});

const write = async (sessionId: string, value = snapshot(sessionId), added = false) => {
  const owner = chatSnapshotOwner(sessionId);
  expect(owner).not.toBeNull();
  await writeChatSnapshot(owner!, value, { added });
};

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

afterEach(async () => {
  vi.restoreAllMocks();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('conversation snapshots', () => {
  it('reads back what was written for the session it names', async () => {
    setLocalSnapshotPrincipal('local');
    await write('ses_a');
    const stored = await readChatSnapshot('ses_a');
    expect(stored?.session.title).toBe('title of ses_a');
    expect(stored?.messages.map((m) => m.id)).toEqual(['m1']);
  });

  it('ignores a record of another build, a corrupt record, and one filed under another session', async () => {
    setLocalSnapshotPrincipal('local');
    const owner = chatSnapshotOwner('ses_a')!;
    await writeChatSnapshot(owner, snapshot('ses_a'), { build: 'https://host/assets/index-OLD.js' });
    expect(await readChatSnapshot('ses_a', 'https://host/assets/index-OLD.js')).not.toBeNull();
    expect(await readChatSnapshot('ses_a')).toBeNull();

    await writeLocalSnapshot(chatSnapshotKey('ses_b'), CHAT_SNAPSHOT_SCHEMA, { messages: 'not rows' });
    expect(await readChatSnapshot('ses_b')).toBeNull();

    await write('ses_c', snapshot('ses_other'));
    expect(await readChatSnapshot('ses_c')).toBeNull();
  });

  it('keeps only the newest stored rows, and the cursor that pages older than them', () => {
    const rows = Array.from({ length: CHAT_SNAPSHOT_ROWS + 10 }, (_, i) => row(`m${i}`));
    const window = chatSnapshotWindow(rows, 'cursor-older-than-m0');
    expect(window.messages).toHaveLength(CHAT_SNAPSHOT_ROWS);
    expect(window.messages[0].id).toBe('m10');
    expect(window.next_before_id).toBe('m10');

    const short = chatSnapshotWindow(rows.slice(0, 3), 'cursor-older-than-m0');
    expect(short.next_before_id).toBe('cursor-older-than-m0');
  });

  it('stores no row of a send still pending: not a Delivery and not the claimed-input projection', () => {
    const window = chatSnapshotWindow([
      row('durable'),
      row('delivery', { type: 'user', state: 'claimed', delivery_id: 'delivery' } as Partial<WorkbenchMessage>),
      row('projection', { type: 'user', projection: 'claimed_delivery' }),
    ], null);
    expect(window.messages.map((m) => m.id)).toEqual(['durable']);
  });

  it('refuses a write whose owner was voided: a forget, a clear, or another reader', async () => {
    setLocalSnapshotPrincipal('local');
    await write('ses_a');

    const forgottenOwner = chatSnapshotOwner('ses_a')!;
    await forgetChatSnapshot('ses_a');
    await writeChatSnapshot(forgottenOwner, snapshot('ses_a'));
    expect(await readChatSnapshot('ses_a')).toBeNull();

    const clearedOwner = chatSnapshotOwner('ses_a')!;
    await clearLocalSnapshots();
    await writeChatSnapshot(clearedOwner, snapshot('ses_a'));
    expect(await readChatSnapshot('ses_a')).toBeNull();

    const aliceOwner = chatSnapshotOwner('ses_a')!;
    setLocalSnapshotPrincipal('remote:bob');
    await writeChatSnapshot(aliceOwner, snapshot('ses_a'));
    await settle();
    expect(await readChatSnapshot('ses_a')).toBeNull();

    // A fresh owner after all of that writes again.
    await write('ses_a');
    expect(await readChatSnapshot('ses_a')).not.toBeNull();
  });

  it(`keeps the ${CHAT_SNAPSHOT_SESSIONS} most recently written conversations`, async () => {
    let now = 1_000;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    setLocalSnapshotPrincipal('local');
    for (let i = 0; i <= CHAT_SNAPSHOT_SESSIONS; i += 1) {
      now += 10;
      await write(`ses_${i}`, snapshot(`ses_${i}`), true);
    }
    expect(await readChatSnapshot('ses_0')).toBeNull();
    expect(await readChatSnapshot('ses_1')).not.toBeNull();
    expect(await readChatSnapshot(`ses_${CHAT_SNAPSHOT_SESSIONS}`)).not.toBeNull();
  });

  it('deletes the conversation of a Session that leaves the Inbox, and only then', async () => {
    setLocalSnapshotPrincipal('local');
    for (const id of ['ses_archived', 'ses_hidden', 'ses_renamed']) await write(id);

    forgetChatSnapshotOnActivity({ session_id: 'ses_archived', event: 'archived' });
    forgetChatSnapshotOnActivity({ session_id: 'ses_hidden', event: 'updated', visibility: 'background' });
    forgetChatSnapshotOnActivity({ session_id: 'ses_renamed', event: 'updated' });
    await settle();

    expect(await readChatSnapshot('ses_archived')).toBeNull();
    expect(await readChatSnapshot('ses_hidden')).toBeNull();
    expect(await readChatSnapshot('ses_renamed')).not.toBeNull();
  });
});
