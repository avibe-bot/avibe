/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import type { WorkbenchSession } from './ApiContext';
import { ApiProvider, useApi } from './ApiContext';
import {
  beginChatSnapshotRequest,
  chatSnapshotOwnerForAnswer,
  readChatSnapshot,
  writeChatSnapshot,
} from '../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../lib/localSnapshots';

// A stored conversation goes when its Session leaves the reader's Inbox
// (docs/plans/web-ui-local-cache.md section 4.4). The signals arrive at the API
// layer, whichever page is open, so that is where they are applied.

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
  await writeChatSnapshot(chatSnapshotOwnerForAnswer(beginChatSnapshotRequest(sessionId))!, {
    session: { id: sessionId, title: sessionId } as WorkbenchSession,
    messages: [],
    next_before_id: null,
    agents: [],
    default_agent_name: null,
    can_chat: true,
  });
  expect(await readChatSnapshot(sessionId)).not.toBeNull();
};

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
