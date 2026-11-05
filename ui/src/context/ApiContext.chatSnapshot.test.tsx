/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import type { WorkbenchMessage, WorkbenchSession } from './ApiContext';
import { ApiProvider, useApi } from './ApiContext';
import { knownChatSnapshot, readChatSnapshot } from '../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../lib/localSnapshots';
import { beginLocalStoreRead, recordLocalStorePage } from '../lib/localStore';

// The conversation a chat paints before its bootstrap answers comes from the
// local message store (docs/plans/local-sync-store.md section 4.3). The API
// layer keeps it: a bootstrap stores what the chat paints, and a Session that
// leaves the reader's Inbox takes its conversation with it, whichever page is
// open.

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readyState = 1;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<(event: MessageEvent) => void>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const bucket = this.listeners.get(type) ?? new Set();
    bucket.add(listener);
    this.listeners.set(type, bucket);
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.readyState = 2;
  }

  emit(type: string, data: unknown): void {
    const event = { data: JSON.stringify(data) } as MessageEvent;
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener(event);
  }
}

const api = { current: null as ReturnType<typeof useApi> | null };
const Subscriber = () => {
  const value = useApi();
  useEffect(() => {
    api.current = value;
    return value.connectWorkbenchEvents({});
  }, [value]);
  return null;
};

const store = async (sessionId: string) => {
  await recordLocalStorePage(
    beginLocalStoreRead(sessionId),
    { kind: 'tail' },
    { messages: [], next_before_id: null },
    { id: sessionId, title: sessionId } as WorkbenchSession,
    { agents: [], default_agent_name: null, can_chat: true, chat_message_font_size: null },
  );
  expect(await readChatSnapshot(sessionId)).not.toBeNull();
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  apiFetch.mockResolvedValue(new Response('{}', { status: 200 }));
  setLocalSnapshotPrincipal('local');
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('conversation snapshot invalidation at the API layer', () => {
  it('keeps what a bootstrap says around its transcript, so the chat paints whole when it is opened again', async () => {
    const row = { id: 'msg_1', session_id: 'ses_a', type: 'result', author: 'agent', text: 'answer', created_at: '2026-11-05T00:00:01.000000Z', updated_at: '2026-11-05T00:00:01.000000Z' } as WorkbenchMessage;
    apiFetch.mockImplementation(async (path: string) => (path === '/api/sessions/ses_a/bootstrap'
      ? json({
        session: { id: 'ses_a', title: 'A', status: 'active' },
        capabilities: { can_chat: false },
        agents: [{ id: 'agent-vibey', name: 'vibey' }],
        default_agent_name: 'vibey',
        config: { ui: { chat_message_font_size: 17 } },
        messages: [row],
        next_after_id: null,
        next_before_id: null,
        queued: [],
        draft: { text: '', updated_at: null },
      })
      : json({})));
    render(<ApiProvider><Subscriber /></ApiProvider>);
    await api.current!.getSessionBootstrap('ses_a');
    await waitFor(async () => expect(await readChatSnapshot('ses_a')).toMatchObject({
      session: { id: 'ses_a', title: 'A' },
      messages: [{ id: 'msg_1', text: 'answer' }],
      next_before_id: null,
      agents: [{ name: 'vibey' }],
      default_agent_name: 'vibey',
      can_chat: false,
      chat_message_font_size: 17,
    }));
    // Held in memory too, so the chat paints in the commit that opens it.
    expect(knownChatSnapshot('ses_a')?.messages.map((message) => message.id)).toEqual(['msg_1']);
  });

  it('deletes a conversation archived or moved to the background, and keeps one only renamed', async () => {
    for (const id of ['ses_archived', 'ses_background', 'ses_renamed']) await store(id);
    render(<ApiProvider><Subscriber /></ApiProvider>);
    const source = FakeEventSource.instances.at(-1)!;

    source.emit('session.activity', { type: 'session.activity', data: { session_id: 'ses_archived', event: 'archived' } });
    source.emit('session.activity', {
      type: 'session.activity',
      data: { session_id: 'ses_background', event: 'updated', visibility: 'background' },
    });
    source.emit('session.activity', { type: 'session.activity', data: { session_id: 'ses_renamed', event: 'updated', title: 'x' } });
    await settle();

    expect(await readChatSnapshot('ses_archived')).toBeNull();
    expect(await readChatSnapshot('ses_background')).toBeNull();
    expect(await readChatSnapshot('ses_renamed')).not.toBeNull();
  });

  it('deletes a conversation a write found archived', async () => {
    await store('ses_stale_tab');
    render(<ApiProvider><Subscriber /></ApiProvider>);
    act(() => api.current!.convergeSessionArchived('ses_stale_tab'));
    await settle();
    expect(await readChatSnapshot('ses_stale_tab')).toBeNull();
  });
});
