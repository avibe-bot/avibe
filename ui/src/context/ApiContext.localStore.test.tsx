/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch, recoverRemoteAuthFromSessionProbe: vi.fn() }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import type { WorkbenchMessage } from './ApiContext';
import { ApiProvider, useApi } from './ApiContext';
import { clearLocalSnapshots, registerLocalSnapshotClearHook, setLocalSnapshotPrincipal } from '../lib/localSnapshots';
import { beginLocalStoreRead, readLocalStoreWindow, recordLocalStorePage } from '../lib/localStore';

// The local message store is written through at the API layer, whichever page
// made the read (docs/plans/local-sync-store.md section 4.4), and emptied there
// when the reader's session ends.

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

const at = (n: number) => `2026-11-05T00:00:${String(n).padStart(2, '0')}.000000Z`;
const row = (n: number, sessionId = 'ses_a'): WorkbenchMessage => ({
  id: `msg_${sessionId}_${n}`,
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
});
const sessionRow = (id = 'ses_a') => ({ id, title: id, status: 'active', pinned: false, last_active_at: at(1), updated_at: at(1) });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const held = async (sessionId = 'ses_a') =>
  ((await readLocalStoreWindow(sessionId, { limit: 100 }))?.messages ?? []).map((message) => message.text);
const seed = (sessionId = 'ses_a') =>
  recordLocalStorePage(beginLocalStoreRead(sessionId), { kind: 'tail' }, { messages: [row(1, sessionId), row(2, sessionId)], next_before_id: null });
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));
// Store writes and wipes land on their own; under a full parallel suite they
// can take far longer than a fixed pause. Wait for the state, not for a time.
const eventually = (assertion: () => Promise<void>) => vi.waitFor(assertion, { timeout: 10_000, interval: 25 });

const api = { current: null as ReturnType<typeof useApi> | null };
const Subscriber = () => {
  const value = useApi();
  useEffect(() => {
    api.current = value;
    return value.connectWorkbenchEvents({});
  }, [value]);
  return null;
};

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  apiFetch.mockResolvedValue(json({}));
  setLocalSnapshotPrincipal('remote:alice');
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('local message store write-through at the API layer', () => {
  it('stores a bootstrap tail and an older page, and forgets a session the server refuses', async () => {
    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/sessions/ses_a/bootstrap') return json({ session: sessionRow(), messages: [row(3), row(4)], next_before_id: 'msg_ses_a_3' });
      if (path.startsWith('/api/sessions/ses_a/messages?before_id=')) return json({ messages: [row(2), row(3)], next_before_id: null });
      if (path === '/api/sessions/ses_b/bootstrap') return json({ error: 'not found' }, 404);
      return json({});
    });
    render(<ApiProvider><Subscriber /></ApiProvider>);
    await api.current!.getSessionBootstrap('ses_a');
    await api.current!.listSessionMessages('ses_a', { beforeId: 'msg_ses_a_4', limit: 50 });
    await eventually(async () => expect(await held()).toEqual(['row 2', 'row 3', 'row 4']));
    expect((await readLocalStoreWindow('ses_a'))?.startReached).toBe(true);

    await seed('ses_b');
    await expect(api.current!.getSessionBootstrap('ses_b')).rejects.toThrow();
    await eventually(async () => expect(await held('ses_b')).toEqual([]));
  });

  it('stores streamed rows for a held session and ends their tail claim when the stream reconnects', async () => {
    await seed();
    render(<ApiProvider><Subscriber /></ApiProvider>);
    const source = FakeEventSource.instances.at(-1)!;
    source.emit('message.new', { type: 'message.new', data: row(3) });
    await eventually(async () => expect(await held()).toEqual(['row 1', 'row 2', 'row 3']));

    // Seeded before this stream connected, so the tail claim did not survive it.
    source.emit('connected', { sub_id: 1 });
    source.emit('message.new', { type: 'message.new', data: row(5) });
    await eventually(async () => expect((await readLocalStoreWindow('ses_a'))?.messages.map((message) => message.text))
      .toEqual(['row 1', 'row 2', 'row 3', 'row 5']));
    const window = await readLocalStoreWindow('ses_a');
    expect(window?.contiguous).toBe(false);
  });

  it('LSYNC-020 forgets a session whose message read the server refuses, and keeps it when the read only failed', async () => {
    await seed();
    render(<ApiProvider><Subscriber /></ApiProvider>);
    apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/sessions/ses_a/messages')) throw new TypeError('Failed to fetch');
      return json({});
    });
    await expect(api.current!.listSessionMessages('ses_a', { tail: true, cache: false })).rejects.toThrow();
    await settle();
    expect(await held()).toEqual(['row 1', 'row 2']);

    apiFetch.mockImplementation(async (path: string) => (
      path.startsWith('/api/sessions/ses_a/messages') ? json({ error: 'forbidden' }, 403) : json({})
    ));
    await expect(api.current!.listSessionMessages('ses_a', { beforeId: 'msg_ses_a_1', limit: 50 })).rejects.toThrow();
    await eventually(async () => expect(await held()).toEqual([]));

    // A silent session read resolves the refusal's body instead of throwing.
    await seed('ses_c');
    apiFetch.mockImplementation(async (path: string) => (path === '/api/sessions/ses_c' ? json({ error: 'not found' }, 404) : json({})));
    await api.current!.getSession('ses_c', { handleError: false });
    await eventually(async () => expect(await held('ses_c')).toEqual([]));

    // A session read that reports the refusal by status rather than throwing.
    await seed('ses_b');
    apiFetch.mockImplementation(async (path: string) => (path === '/api/sessions/ses_b' ? json({ error: 'not found' }, 404) : json({})));
    expect((await api.current!.getSessionResult('ses_b')).status).toBe(404);
    await eventually(async () => expect(await held('ses_b')).toEqual([]));
  });

  it('ends tail claims when the stream fails, so a tail read during the outage does not join the stored one', async () => {
    let tailFrom = 1;
    apiFetch.mockImplementation(async (path: string) => (
      path === '/api/sessions/ses_a/bootstrap'
        ? json({ session: sessionRow(), messages: [row(tailFrom), row(tailFrom + 1)], next_before_id: tailFrom > 1 ? 'cursor' : null })
        : json({})
    ));
    render(<ApiProvider><Subscriber /></ApiProvider>);
    const source = FakeEventSource.instances.at(-1)!;
    source.emit('connected', { sub_id: 1 });
    source.emit('workbench.events.bridge.status', { type: 'workbench.events.bridge.status', data: { connected: true } });
    await api.current!.getSessionBootstrap('ses_a');
    await eventually(async () => expect(await held()).toEqual(['row 1', 'row 2']));
    // On the connected stream the tail holds its claim: a streamed row extends it.
    source.emit('message.new', { type: 'message.new', data: row(3) });
    await eventually(async () => expect((await readLocalStoreWindow('ses_a'))?.messages.length).toBe(3));
    expect((await readLocalStoreWindow('ses_a'))?.contiguous).toBe(true);

    act(() => source.onerror?.(new Event('error')));
    // Rows 4-6 arrived on the server while the stream was down.
    tailFrom = 7;
    await api.current!.getSessionBootstrap('ses_a');
    await eventually(async () => expect((await readLocalStoreWindow('ses_a'))?.messages.map((message) => message.text))
      .toEqual(['row 1', 'row 2', 'row 3', 'row 7', 'row 8']));
    const window = await readLocalStoreWindow('ses_a');
    expect(window?.contiguous).toBe(false);
  });

  it('does not let a cached read that went out before another tab cleared the store write after it', async () => {
    await seed();
    let answer!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      answer = resolve;
    });
    apiFetch.mockImplementation(async (path: string) => (path.startsWith('/api/sessions/ses_a/messages') ? pending : json({})));
    render(<ApiProvider><Subscriber /></ApiProvider>);
    const first = api.current!.listSessionMessages('ses_a', { tail: true, limit: 50 });

    // Another tab clears on an authorization change this tab never heard of.
    vi.resetModules();
    const otherSnapshots = await import('../lib/localSnapshots');
    const otherStore = await import('../lib/localStore');
    otherSnapshots.setLocalSnapshotPrincipal('remote:alice');
    await otherStore.readLocalStoreWindow('ses_a');
    await otherSnapshots.clearLocalSnapshots();

    answer(json({ messages: [row(1), row(2), row(3)], next_before_id: null }));
    // A second caller shares the cached read that went out before the clear.
    const second = api.current!.listSessionMessages('ses_a', { tail: true, limit: 50 });
    await Promise.all([first, second]);
    await settle();
    expect(await held()).toEqual([]);
    otherSnapshots.setLocalSnapshotPrincipal(null);
  });

  it('forgets a session archived while the reader watches', async () => {
    await seed();
    render(<ApiProvider><Subscriber /></ApiProvider>);
    FakeEventSource.instances.at(-1)!.emit('session.activity', { type: 'session.activity', data: { session_id: 'ses_a', event: 'archived' } });
    await eventually(async () => expect(await held()).toEqual([]));
  });
});

describe('a revoked reader empties the store', () => {
  it('LSYNC-006 wipes when /api/session reports the reader revoked', async () => {
    render(<ApiProvider><Subscriber /></ApiProvider>);
    await seed();
    apiFetch.mockImplementation(async (path: string) => (
      path === '/api/session'
        ? json({ remote: true, authenticated: true, authorization_state: 'revoked', author_id: 'remote:alice' })
        : json({})
    ));
    await api.current!.getAuthSession();
    setLocalSnapshotPrincipal('remote:alice');
    await eventually(async () => expect(await held()).toEqual([]));
  });
});

describe('an access change no authorization.changed delivered', () => {
  // Every tab was closed, or the event stream was down, when the reader lost
  // access: the session check compares what /api/session says they may read.
  const DIGEST_KEY = 'avibe.access-digest.v1';
  const current = (author: string, digest: string) => json({
    remote: true,
    authenticated: true,
    authorization_state: 'current',
    instance_role: 'editor',
    author_id: author,
    access_digest: digest,
  });
  const serveSession = (answer: () => Response) =>
    apiFetch.mockImplementation(async (path: string) => (path === '/api/session' ? answer() : json({})));
  const recorded = () => (JSON.parse(window.localStorage.getItem(DIGEST_KEY) ?? 'null') as { digest?: string } | null)?.digest;

  afterEach(() => {
    window.localStorage.removeItem(DIGEST_KEY);
  });

  it('LSYNC-023 clears the stored history when a session check finds a change no event delivered', async () => {
    render(<ApiProvider><Subscriber /></ApiProvider>);
    await seed();
    const changed = vi.fn();
    const stop = api.current!.connectWorkbenchEvents({ onAuthorizationChanged: changed });
    // Another reader's digest, then this reader's first and the same again: no change.
    serveSession(() => current('remote:bob', 'digest-bob'));
    await api.current!.getAuthSession();
    let digest = 'digest-1';
    serveSession(() => current('remote:alice', digest));
    await api.current!.getAuthSession();
    await api.current!.getAuthSession();
    await settle();
    expect(await held()).toEqual(['row 1', 'row 2']);
    expect(changed).not.toHaveBeenCalled();

    // Alice lost a project while no tab was listening.
    digest = 'digest-2';
    await api.current!.getAuthSession();
    await eventually(async () => expect(await held()).toEqual([]));
    expect(changed).toHaveBeenCalledTimes(1);
    expect(recorded()).toBe('digest-2');
    stop();
  });

  it("LSYNC-023 checks a remote reader's access again after a stream gap, and costs a local reader nothing", async () => {
    render(<ApiProvider><Subscriber /></ApiProvider>);
    await seed();
    let digest = 'digest-1';
    serveSession(() => current('remote:alice', digest));
    await api.current!.getAuthSession();
    // The stream was down when the change happened; it reconnects.
    digest = 'digest-2';
    FakeEventSource.instances.at(-1)!.emit('connected', { sub_id: 2 });
    await eventually(async () => expect(await held()).toEqual([]));

    // A local reader has no access digest, so a gap reads no session for it.
    serveSession(() => json({ remote: false, author_id: 'local' }));
    await api.current!.getAuthSession();
    apiFetch.mockClear();
    FakeEventSource.instances.at(-1)!.emit('connected', { sub_id: 3 });
    await settle();
    expect(apiFetch.mock.calls.filter(([path]) => path === '/api/session')).toEqual([]);
  });

  it('records the new digest only once the clear it calls for is done, so a page closing first clears again', async () => {
    render(<ApiProvider><Subscriber /></ApiProvider>);
    let digest = 'digest-1';
    serveSession(() => current('remote:alice', digest));
    await api.current!.getAuthSession();
    // A clear still under way when the page could close.
    let finish!: () => void;
    const clearing = new Promise<void>((resolve) => {
      finish = resolve;
    });
    registerLocalSnapshotClearHook(() => clearing);
    digest = 'digest-2';
    await api.current!.getAuthSession();
    await settle();
    expect(recorded()).toBe('digest-1');
    finish();
    await eventually(async () => expect(recorded()).toBe('digest-2'));
  });
});

describe('an expired session empties the store', () => {
  it('LSYNC-021 wipes on a login-required answer, and keeps everything when the session read only failed', async () => {
    render(<ApiProvider><Subscriber /></ApiProvider>);
    await seed();

    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/session') throw new TypeError('Failed to fetch');
      return json({});
    });
    await expect(api.current!.getAuthSession()).rejects.toThrow();
    await settle();
    expect(await held()).toEqual(['row 1', 'row 2']);

    apiFetch.mockImplementation(async (path: string) => (
      path === '/api/session' ? json({ remote: true, authenticated: true, authorization_state: 'current', author_id: 'remote:alice' }) : json({})
    ));
    await api.current!.getAuthSession();
    await settle();
    expect(await held()).toEqual(['row 1', 'row 2']);

    apiFetch.mockImplementation(async (path: string) => (
      path === '/api/session' ? json({ remote: true, authenticated: false }) : json({})
    ));
    await api.current!.getAuthSession();
    await eventually(async () => expect(await held()).toEqual([]));
  });
});
