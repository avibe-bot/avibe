import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { WorkbenchMessage, WorkbenchSession } from '../context/ApiContext';
import {
  beginChatSnapshotRequest,
  chatSnapshotOwnerForAnswer,
  forgetChatSnapshot,
  knownChatSnapshot,
  readChatSnapshot,
  writeChatSnapshot,
  type ChatSnapshot,
} from './chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from './localSnapshots';

// What this document already holds of a conversation is readable synchronously,
// so a chat it opens again paints in its first frame. It may only ever say what
// the store would: the current reader's, as recent as this document knows, and
// gone when the store's copy is void.

// The store's cap (docs/plans/web-ui-local-cache.md section 4.1).
const KEPT_CONVERSATIONS = 40;

const ownerFor = (sessionId: string) => chatSnapshotOwnerForAnswer(beginChatSnapshotRequest(sessionId))!;

const snapshot = (sessionId: string, text = 'answer'): ChatSnapshot => ({
  session: { id: sessionId, title: `title of ${sessionId}` } as WorkbenchSession,
  messages: [{
    id: 'm1',
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
    created_at: '2026-11-01T08:00:00Z',
    updated_at: '2026-11-01T08:00:00Z',
    delivered_at: '2026-11-01T08:00:00Z',
    read_at: null,
  } as WorkbenchMessage],
  next_before_id: null,
  agents: [],
  default_agent_name: null,
  can_chat: true,
  chat_message_font_size: null,
});

const knownText = (sessionId: string) => knownChatSnapshot(sessionId)?.messages[0]?.text ?? null;

// A later load of the page: the reader is named again, and only the store has
// the conversations an earlier load wrote.
const asALaterLoad = () => {
  setLocalSnapshotPrincipal(null);
  setLocalSnapshotPrincipal('local');
};

beforeEach(() => setLocalSnapshotPrincipal('local'));

afterEach(async () => {
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('conversations held in memory', () => {
  it('knows a conversation the moment it is written', () => {
    void writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a', 'written'));
    expect(knownText('ses_a')).toBe('written');
    expect(knownChatSnapshot('ses_b')).toBeNull();
  });

  it('knows a conversation once the store has been read for it', async () => {
    await writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a', 'stored earlier'));
    asALaterLoad();
    expect(knownChatSnapshot('ses_a')).toBeNull();

    await readChatSnapshot('ses_a');
    expect(knownText('ses_a')).toBe('stored earlier');
  });

  it('keeps what this document wrote over an older stored copy a read brings back', async () => {
    await writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a', 'stored earlier'));
    asALaterLoad();
    const read = readChatSnapshot('ses_a');
    void writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a', 'written since'));

    expect((await read)?.messages[0].text).toBe('stored earlier');
    expect(knownText('ses_a')).toBe('written since');
  });

  it('holds nothing a read found for a reader who has left', async () => {
    await writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a', 'stored earlier'));
    asALaterLoad();
    const read = readChatSnapshot('ses_a');
    setLocalSnapshotPrincipal('someone-else');

    await read;
    expect(knownChatSnapshot('ses_a')).toBeNull();
  });

  it('forgets everything when the reader changes, even back to the same one', () => {
    void writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a'));
    setLocalSnapshotPrincipal('someone-else');
    expect(knownChatSnapshot('ses_a')).toBeNull();
    setLocalSnapshotPrincipal('local');
    expect(knownChatSnapshot('ses_a')).toBeNull();
  });

  it('forgets everything when the store is cleared', async () => {
    void writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a'));
    await clearLocalSnapshots();
    expect(knownChatSnapshot('ses_a')).toBeNull();
  });

  it('forgets a forgotten conversation, and only that one', () => {
    void writeChatSnapshot(ownerFor('ses_a'), snapshot('ses_a'));
    void writeChatSnapshot(ownerFor('ses_b'), snapshot('ses_b'));
    void forgetChatSnapshot('ses_a');
    expect(knownChatSnapshot('ses_a')).toBeNull();
    expect(knownText('ses_b')).toBe('answer');
  });

  it(`keeps the ${KEPT_CONVERSATIONS} most recently written conversations`, () => {
    for (let i = 0; i <= KEPT_CONVERSATIONS; i += 1) void writeChatSnapshot(ownerFor(`ses_${i}`), snapshot(`ses_${i}`));
    expect(knownChatSnapshot('ses_0')).toBeNull();
    expect(knownChatSnapshot('ses_1')).not.toBeNull();
    expect(knownChatSnapshot(`ses_${KEPT_CONVERSATIONS}`)).not.toBeNull();

    // Writing one again makes it the most recent.
    void writeChatSnapshot(ownerFor('ses_1'), snapshot('ses_1'));
    void writeChatSnapshot(ownerFor('ses_extra'), snapshot('ses_extra'));
    expect(knownChatSnapshot('ses_1')).not.toBeNull();
    expect(knownChatSnapshot('ses_2')).toBeNull();
  });
});
