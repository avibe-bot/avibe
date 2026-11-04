// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchSession } from '../context/ApiContext';
import type { ChatSnapshot } from './chatSnapshots';

// The prefetch fills in conversations the reader has not opened yet, in idle
// time. What it must never do is more than that: send anything but the declared
// prefetch read, fetch past its budget, run on a metered connection or a hidden
// page, overwrite a chat the reader opened, or store an answer for a reader who
// is no longer the one signed in.

const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('./apiFetch', () => ({ apiFetch }));

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void };
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
};

const BUDGET = 3; // At most three at a time (plan section 4.2).

const bootstrapResponse = (sessionId: string, title = `live ${sessionId}`) => Response.json({
  session: { id: sessionId, title },
  capabilities: { can_chat: true },
  agents: [],
  default_agent_name: null,
  config: null,
  messages: [],
  next_after_id: null,
  next_before_id: null,
  queued: [],
  draft: { text: '', updated_at: null },
  turn_state: {},
});

const stored = (sessionId: string, title: string): ChatSnapshot => ({
  session: { id: sessionId, title } as WorkbenchSession,
  messages: [],
  next_before_id: null,
  agents: [],
  default_agent_name: null,
  can_chat: true,
});

const sessionOf = (path: string) => decodeURIComponent(path.split('/')[3]);

// Every test starts a fresh document: the prefetch remembers what it tried.
let prefetch: typeof import('./chatPrefetch');
let snapshots: typeof import('./chatSnapshots');
let store: typeof import('./localSnapshots');
const storeAs = async (sessionId: string, title: string) => {
  const owner = snapshots.chatSnapshotOwnerForAnswer(snapshots.beginChatSnapshotRequest(sessionId))!;
  await snapshots.writeChatSnapshot(owner, stored(sessionId, title));
};
const titleOf = async (sessionId: string) => (await snapshots.readChatSnapshot(sessionId))?.session.title ?? null;
const requested = () => apiFetch.mock.calls.map(([path]) => sessionOf(path as string));

let idle: Array<() => void> = [];
const runIdle = async () => {
  const pending = idle;
  idle = [];
  pending.forEach((run) => run());
  await new Promise((resolve) => setTimeout(resolve, 30));
};

const setConnection = (connection: object | undefined) => {
  Object.defineProperty(navigator, 'connection', { configurable: true, value: connection });
};
const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
};

beforeEach(async () => {
  idle = [];
  vi.stubGlobal('requestIdleCallback', (run: () => void) => {
    idle.push(run);
    return idle.length;
  });
  apiFetch.mockReset();
  vi.resetModules();
  [prefetch, snapshots, store] = await Promise.all([
    import('./chatPrefetch'),
    import('./chatSnapshots'),
    import('./localSnapshots'),
  ]);
  store.setLocalSnapshotPrincipal('local');
});

afterEach(async () => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, 'connection');
  Reflect.deleteProperty(document, 'visibilityState');
  store.setLocalSnapshotPrincipal(null);
  await store.clearLocalSnapshots();
});

describe('conversation prefetch', () => {
  it('sends only the bootstrap read, declared a prefetch, and stores its answer', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    prefetch.prefetchChatSnapshots(['ses a']);
    await runIdle();

    expect(apiFetch.mock.calls).toEqual([
      ['/api/sessions/ses%20a/bootstrap', { headers: { 'X-Avibe-Prefetch': '1' } }],
    ]);
    expect(await titleOf('ses a')).toBe('live ses a');
  });

  it('takes the most recent Sessions with no snapshot, in idle time, three at a time', async () => {
    await storeAs('ses_1', 'opened before');
    const responses = new Map<string, Deferred<Response>>();
    let inFlight = 0;
    let mostInFlight = 0;
    apiFetch.mockImplementation((path: string) => {
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      const response = deferred<Response>();
      responses.set(sessionOf(path), response);
      return response.promise.finally(() => {
        inFlight -= 1;
      });
    });

    prefetch.prefetchChatSnapshots(['ses_0', 'ses_1', 'ses_2', 'ses_3', 'ses_4', 'ses_5', 'ses_6']);
    expect(apiFetch).not.toHaveBeenCalled();
    await runIdle();
    // ses_1 already has a snapshot, so it takes a slot only to find that out.
    expect(requested()).toEqual(['ses_0', 'ses_2']);
    await runIdle();
    expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3']);
    await runIdle();
    expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3']);

    responses.get('ses_0')!.resolve(bootstrapResponse('ses_0'));
    await runIdle();
    await runIdle();
    expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3', 'ses_4']);
    for (const id of ['ses_2', 'ses_3', 'ses_4']) responses.get(id)!.resolve(bootstrapResponse(id));
    await runIdle();
    await runIdle();

    expect(mostInFlight).toBe(BUDGET);
    expect(await titleOf('ses_0')).toBe('live ses_0');
    expect(await titleOf('ses_4')).toBe('live ses_4');
    expect(await titleOf('ses_1')).toBe('opened before');
    expect(await titleOf('ses_5')).toBeNull();
  });

  it('stays off a metered or data-saving connection and a hidden page, and runs once offered under good conditions', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    for (const connection of [{ type: 'cellular' }, { type: 'wifi', saveData: true }, { metered: true }]) {
      setConnection(connection);
      prefetch.prefetchChatSnapshots(['ses_a']);
      await runIdle();
    }
    setConnection({ type: 'wifi' });
    setVisibility('hidden');
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    expect(apiFetch).not.toHaveBeenCalled();

    setVisibility('visible');
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    expect(requested()).toEqual(['ses_a']);
  });

  it('runs where the Network Information API is missing', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    setConnection(undefined);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    expect(requested()).toEqual(['ses_a']);
  });

  it('gives back a queued Session when the connection turns metered before the browser is idle', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    prefetch.prefetchChatSnapshots(['ses_a']);
    setConnection({ type: 'cellular' });
    await runIdle();
    expect(apiFetch).not.toHaveBeenCalled();

    setConnection({ type: 'ethernet' });
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    expect(requested()).toEqual(['ses_a']);
  });

  it('tries a Session once per document, and fails silently', async () => {
    apiFetch.mockResolvedValue(Response.json({ error: 'unavailable' }, { status: 503 }));
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    expect(requested()).toEqual(['ses_a']);
    expect(await titleOf('ses_a')).toBeNull();
  });

  it('leaves alone a chat the reader opened while the prefetch was out', async () => {
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await storeAs('ses_a', 'opened by the reader');
    response.resolve(bootstrapResponse('ses_a', 'prefetched'));
    await runIdle();
    expect(await titleOf('ses_a')).toBe('opened by the reader');
  });

  it('stores nothing when the reader changed while the request was out', async () => {
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    store.setLocalSnapshotPrincipal('remote:bob');
    response.resolve(bootstrapResponse('ses_a', 'answered for the last reader'));
    await runIdle();
    expect(await titleOf('ses_a')).toBeNull();
  });

  it('deletes a record stored meanwhile when the server answers 404', async () => {
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await storeAs('ses_a', 'stored meanwhile');
    response.resolve(Response.json({ error: 'Session not found' }, { status: 404 }));
    await runIdle();
    expect(await titleOf('ses_a')).toBeNull();
  });
});
