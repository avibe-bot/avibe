import 'fake-indexeddb/auto';

import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchMessage, WorkbenchSession } from '../context/ApiContext';
import {
  beginChatSnapshotRequest,
  chatSnapshotOwnerForAnswer,
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
// not just say: rows of a send still pending, a corrupt or misfiled record, or a
// write that lands after the store was voided. (Another build's record is
// covered in chatSnapshots.build.test.ts.)

// The plan's caps (docs/plans/web-ui-local-cache.md section 4.1).
const WINDOW_ROWS = 50;
const KEPT_CONVERSATIONS = 40;

// Ownership as production takes it: when the request starts, settled when the
// answer lands.
const ownerFor = (sessionId: string) => chatSnapshotOwnerForAnswer(beginChatSnapshotRequest(sessionId))!;

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
  await writeChatSnapshot(ownerFor(sessionId), value, { added });
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

  it('ignores a corrupt record and one filed under another session', async () => {
    setLocalSnapshotPrincipal('local');
    await write('ses_b');
    expect(await readChatSnapshot('ses_b')).not.toBeNull();
    await writeLocalSnapshot('chat:ses_b', 1, { messages: 'not rows' });
    expect(await readChatSnapshot('ses_b')).toBeNull();

    await write('ses_c', snapshot('ses_other'));
    expect(await readChatSnapshot('ses_c')).toBeNull();
  });

  it('keeps only the newest stored rows, and the cursor that pages older than them', () => {
    const rows = Array.from({ length: WINDOW_ROWS + 10 }, (_, i) => row(`m${i}`));
    const window = chatSnapshotWindow(rows, 'cursor-older-than-m0');
    expect(window.messages).toHaveLength(WINDOW_ROWS);
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

    const forgottenOwner = ownerFor('ses_a');
    await forgetChatSnapshot('ses_a');
    await writeChatSnapshot(forgottenOwner, snapshot('ses_a'));
    expect(await readChatSnapshot('ses_a')).toBeNull();

    const clearedOwner = ownerFor('ses_a');
    await clearLocalSnapshots();
    await writeChatSnapshot(clearedOwner, snapshot('ses_a'));
    expect(await readChatSnapshot('ses_a')).toBeNull();

    const aliceOwner = ownerFor('ses_a');
    setLocalSnapshotPrincipal('remote:bob');
    await writeChatSnapshot(aliceOwner, snapshot('ses_a'));
    await settle();
    expect(await readChatSnapshot('ses_a')).toBeNull();

    // A fresh owner after all of that writes again.
    await write('ses_a');
    expect(await readChatSnapshot('ses_a')).not.toBeNull();
  });

  it('returns nothing from a read that a forget overtook', async () => {
    setLocalSnapshotPrincipal('local');
    await write('ses_a');
    expect(await readChatSnapshot('ses_a')).not.toBeNull();

    const overtaken = readChatSnapshot('ses_a');
    void forgetChatSnapshot('ses_a');
    expect(await overtaken).toBeNull();
  });

  it('gives an answer to the reader named after its request started, unless the store moved meanwhile', async () => {
    // A cold start sends the bootstrap before the reader is named.
    const coldStart = beginChatSnapshotRequest('ses_a');
    expect(coldStart.principal).toBeNull();
    setLocalSnapshotPrincipal('local');
    expect(chatSnapshotOwnerForAnswer(coldStart)?.principal).toBe('local');

    const beforeClear = beginChatSnapshotRequest('ses_a');
    await clearLocalSnapshots();
    expect(chatSnapshotOwnerForAnswer(beforeClear)).toBeNull();

    const beforeForget = beginChatSnapshotRequest('ses_a');
    await forgetChatSnapshot('ses_a');
    expect(chatSnapshotOwnerForAnswer(beforeForget)).toBeNull();

    const beforeSwitch = beginChatSnapshotRequest('ses_a');
    setLocalSnapshotPrincipal('remote:bob');
    expect(chatSnapshotOwnerForAnswer(beforeSwitch)).toBeNull();

    // Nobody named at either end: nothing to store under.
    setLocalSnapshotPrincipal(null);
    expect(chatSnapshotOwnerForAnswer(beginChatSnapshotRequest('ses_a'))).toBeNull();
  });

  it(`keeps the ${KEPT_CONVERSATIONS} most recently written conversations`, async () => {
    let now = 1_000;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    setLocalSnapshotPrincipal('local');
    for (let i = 0; i <= KEPT_CONVERSATIONS; i += 1) {
      now += 10;
      await write(`ses_${i}`, snapshot(`ses_${i}`), true);
    }
    expect(await readChatSnapshot('ses_0')).toBeNull();
    expect(await readChatSnapshot('ses_1')).not.toBeNull();
    expect(await readChatSnapshot(`ses_${KEPT_CONVERSATIONS}`)).not.toBeNull();
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
