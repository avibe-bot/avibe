// @vitest-environment jsdom
import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchSession } from '../context/ApiContext';

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


const sessionOf = (path: string) => decodeURIComponent(path.split('/')[3]);
// A prefetched answer that carries a row, so a case can tell its write has landed
// (the row and the session are written in one transaction).
const answerWithRow = async (sessionId: string, title: string) => Response.json({
  ...(await bootstrapResponse(sessionId, title).json()),
  messages: [{
    id: `msg_${sessionId}_1`, session_id: sessionId, type: 'result', author: 'agent', text: 'prefetched row', content: {}, metadata: {},
    created_at: '2026-11-05T00:00:01.000000Z', updated_at: '2026-11-05T00:00:01.000000Z', delivered_at: null,
  }],
});

// Every test starts a fresh document: the prefetch remembers what it tried.
let prefetch: typeof import('./chatPrefetch');
let snapshots: typeof import('./chatSnapshots');
let store: typeof import('./localSnapshots');
let messageStore: typeof import('./localStore');
// The reader opens the chat: its bootstrap is stored, as the API layer stores it.
const storeAs = async (sessionId: string, title: string) => {
  await messageStore.recordLocalStorePage(
    messageStore.beginLocalStoreRead(sessionId),
    { kind: 'tail' },
    { messages: [], next_before_id: null },
    { id: sessionId, title } as WorkbenchSession,
    { agents: [], default_agent_name: null, can_chat: true, chat_message_font_size: null },
  );
};
const titleOf = async (sessionId: string) => (await snapshots.readChatSnapshot(sessionId))?.session.title ?? null;
const rowsOf = async (sessionId: string) =>
  ((await messageStore.readLocalStoreWindow(sessionId))?.messages ?? []).map((row) => row.text);
// The stored session row, whether or not the chat could paint from what is stored.
const storedTitleOf = async (sessionId: string) => (await messageStore.readLocalStoreWindow(sessionId))?.session?.title ?? null;
// The reader opens the chat while a prefetch of it is out: a newer tail whose
// reach starts at its own (newest) row, so the older prefetched row, outside
// that reach, still lands and shows that the prefetch's write did.
const readerOpens = (sessionId: string, title: string) => messageStore.recordLocalStorePage(
  messageStore.beginLocalStoreRead(sessionId),
  { kind: 'tail' },
  {
    messages: [{
      id: `msg_${sessionId}_5`, session_id: sessionId, type: 'result', author: 'agent', text: 'reader row', content: {}, metadata: {},
      created_at: '2026-11-05T00:00:05.000000Z', updated_at: '2026-11-05T00:00:05.000000Z', delivered_at: null,
    }],
    next_before_id: `msg_${sessionId}_5`,
  } as never,
  { id: sessionId, title } as WorkbenchSession,
  { agents: [], default_agent_name: null, can_chat: true, chat_message_font_size: null },
);
// The prefetch reads the store before its request goes out; act "while it is
// out" only once it is.
const requestOut = () => vi.waitFor(() => expect(apiFetch).toHaveBeenCalled(), SETTLE);
const requested = () => apiFetch.mock.calls.map(([path]) => sessionOf(path as string));

let idle: Array<() => void> = [];
const runIdle = async () => {
  const pending = idle;
  idle = [];
  pending.forEach((run) => run());
  await new Promise((resolve) => setTimeout(resolve, 30));
};
// Each prefetch reads the store before it fetches, which under a loaded run can
// outlast any fixed pause: wait for the state instead. ``idleUntil`` also runs
// the idle work the prefetch asks for meanwhile, so use it only where running
// more of it cannot overshoot the state awaited.
const SETTLE = { timeout: 10_000, interval: 25 };
const idleUntil = (assertion: () => void | Promise<void>) => vi.waitFor(async () => {
  await runIdle();
  await assertion();
}, SETTLE);

// Network Information: an EventTarget that announces changes to its fields.
class FakeConnection extends EventTarget {
  type?: string;
  saveData?: boolean;
  metered?: boolean;
  set(fields: { type?: string; saveData?: boolean; metered?: boolean }) {
    Object.assign(this, { type: undefined, saveData: undefined, metered: undefined }, fields);
    this.dispatchEvent(new Event('change'));
  }
}
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
  [prefetch, snapshots, store, messageStore] = await Promise.all([
    import('./chatPrefetch'),
    import('./chatSnapshots'),
    import('./localSnapshots'),
    import('./localStore'),
  ]);
  store.setLocalSnapshotPrincipal('local');
  // The first read in a fresh document opens and claims the message store; do
  // it here, so a case times the prefetch rather than that.
  await messageStore.readLocalStoreWindow('ses_warm_up');
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
    // The first read in a fresh document also opens the store; wait for it
    // rather than for a fixed time.
    await vi.waitFor(() => expect(apiFetch).toHaveBeenCalled());
    await vi.waitFor(async () => expect(await titleOf('ses a')).toBe('live ses a'));

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
    // No more idle work runs until this is seen, so nothing can overtake it.
    await vi.waitFor(() => expect(requested()).toEqual(['ses_0', 'ses_2']), SETTLE);
    // From here on every slot is taken until a response lands, so running more
    // idle work starts nothing more.
    await idleUntil(() => expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3']));
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3']), SETTLE);

    responses.get('ses_0')!.resolve(bootstrapResponse('ses_0'));
    await idleUntil(() => expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3', 'ses_4']));
    for (const id of ['ses_2', 'ses_3', 'ses_4']) responses.get(id)!.resolve(bootstrapResponse(id));
    await idleUntil(async () => expect(await titleOf('ses_4')).toBe('live ses_4'));

    expect(mostInFlight).toBe(BUDGET);
    expect(await titleOf('ses_0')).toBe('live ses_0');
    expect(await titleOf('ses_4')).toBe('live ses_4');
    expect(await titleOf('ses_1')).toBe('opened before');
    // Past the five newest, the rest of the Inbox's first page is warmed too.
    await idleUntil(() => expect(requested()).toEqual(['ses_0', 'ses_2', 'ses_3', 'ses_4', 'ses_5', 'ses_6']));
  });

  it("warms the Inbox's first page and nothing past it, once per document", async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    const feed = Array.from({ length: prefetch.CHAT_PREFETCH_CANDIDATES + 2 }, (_, i) => `ses_${i}`);
    prefetch.prefetchChatSnapshots(feed);
    await idleUntil(() => expect(requested()).toHaveLength(prefetch.CHAT_PREFETCH_CANDIDATES));
    await idleUntil(async () => expect(await titleOf(feed[prefetch.CHAT_PREFETCH_CANDIDATES - 1])).not.toBeNull());
    expect(requested()).toEqual(feed.slice(0, prefetch.CHAT_PREFETCH_CANDIDATES));

    // Offered again (the feed re-rendered), nothing is fetched a second time.
    prefetch.prefetchChatSnapshots(feed);
    for (let round = 0; round < 3; round += 1) await runIdle();
    expect(requested()).toHaveLength(prefetch.CHAT_PREFETCH_CANDIDATES);
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
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a']), SETTLE);
  });

  it('runs where the Network Information API is missing', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    setConnection(undefined);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a']), SETTLE);
  });

  it('gives back a queued Session when the connection turns metered before the browser is idle, and takes it when it recovers', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    const connection = new FakeConnection();
    connection.set({ type: 'wifi' });
    setConnection(connection);
    prefetch.prefetchChatSnapshots(['ses_a']);
    connection.set({ type: 'cellular' });
    await runIdle();
    expect(apiFetch).not.toHaveBeenCalled();

    // Nothing offers it again but the connection itself.
    connection.set({ type: 'ethernet' });
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a']), SETTLE);
  });

  it('offers again when a page loaded in the background is shown', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    setVisibility('hidden');
    prefetch.prefetchChatSnapshots(['ses_a', 'ses_b']);
    await runIdle();
    expect(apiFetch).not.toHaveBeenCalled();

    setVisibility('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a', 'ses_b']), SETTLE);
  });

  it('offers again when a data-saving connection stops saving data', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    const connection = new FakeConnection();
    connection.set({ type: 'wifi', saveData: true });
    setConnection(connection);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    expect(apiFetch).not.toHaveBeenCalled();

    connection.set({ type: 'wifi' });
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a']), SETTLE);
  });

  it('tries a Session once per document, and fails silently', async () => {
    apiFetch.mockResolvedValue(Response.json({ error: 'unavailable' }, { status: 503 }));
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a']));
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_a']), SETTLE);
    expect(await titleOf('ses_a')).toBeNull();
  });

  it('leaves alone a chat the reader opened while the prefetch was out', async () => {
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await requestOut();
    await readerOpens('ses_a', 'opened by the reader');
    response.resolve(await answerWithRow('ses_a', 'prefetched'));
    await vi.waitFor(async () => expect(await rowsOf('ses_a')).toEqual(['prefetched row', 'reader row']), SETTLE);
    expect(await storedTitleOf('ses_a')).toBe('opened by the reader');
  });

  // The main Bot's conversation matters most: it is always taken, first, and
  // refreshed even when stored, so opening it is instant and current.
  it('takes the main Bot conversation first and refreshes it, even stored and not among the recent', async () => {
    await storeAs('ses_main', 'stored before');
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));

    prefetch.prefetchChatSnapshots(['ses_0', 'ses_1', 'ses_2'], { pinned: 'ses_main' });
    await runIdle();

    expect(requested()).toEqual(['ses_main', 'ses_0', 'ses_1']);
    expect(apiFetch.mock.calls[0]).toEqual(['/api/sessions/ses_main/bootstrap', { headers: { 'X-Avibe-Prefetch': '1' } }]);
    await runIdle();
    await vi.waitFor(async () => expect(await titleOf('ses_main')).toBe('live ses_main'), SETTLE);

    // Once per document, like every other Session.
    prefetch.prefetchChatSnapshots(['ses_0'], { pinned: 'ses_main' });
    await runIdle();
    expect(requested().filter((id) => id === 'ses_main')).toHaveLength(1);
  });

  it('goes ahead of recent Sessions already queued when the main Bot becomes known later, and once', async () => {
    const main = deferred<Response>();
    apiFetch.mockImplementation((path: string) => (
      sessionOf(path) === 'ses_main' ? main.promise : Promise.resolve(bootstrapResponse(sessionOf(path)))
    ));
    prefetch.prefetchChatSnapshots(['ses_0', 'ses_1', 'ses_2', 'ses_main']);
    prefetch.prefetchChatSnapshots(['ses_0', 'ses_1', 'ses_2', 'ses_main'], { pinned: 'ses_main' });
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_main', 'ses_0', 'ses_1']), SETTLE);
    // Its place among the recent was given up for the pinned one, so a slow
    // refresh is not fetched a second time.
    for (let round = 0; round < 4; round += 1) await runIdle();
    expect(requested()).toEqual(['ses_main', 'ses_0', 'ses_1', 'ses_2']);
    main.resolve(bootstrapResponse('ses_main'));
    await runIdle();
    await vi.waitFor(async () => expect(await titleOf('ses_main')).toBe('live ses_main'), SETTLE);
  });

  it.each([
    { when: 'when offered', openAt: 'offer' },
    { when: 'before its turn comes', openAt: 'queue' },
  ])('leaves the main Bot conversation alone while it is the chat open $when', async ({ openAt }) => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    const recent = ['ses_0', 'ses_main'];
    prefetch.prefetchChatSnapshots(recent, { pinned: 'ses_main', open: openAt === 'offer' ? 'ses_main' : null });
    if (openAt === 'queue') prefetch.prefetchChatSnapshots(recent, { pinned: 'ses_main', open: 'ses_main' });
    await runIdle();
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_0']), SETTLE);
  });

  it('leaves alone a main Bot chat the reader stored while its refresh was out', async () => {
    await storeAs('ses_main', 'stored before');
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots([], { pinned: 'ses_main' });
    await runIdle();
    await requestOut();
    await readerOpens('ses_main', 'opened by the reader');
    response.resolve(await answerWithRow('ses_main', 'prefetched'));
    await vi.waitFor(async () => expect(await rowsOf('ses_main')).toEqual(['prefetched row', 'reader row']), SETTLE);
    expect(await storedTitleOf('ses_main')).toBe('opened by the reader');
  });

  it('gives the main Bot conversation back when conditions turn, and refreshes it once they recover', async () => {
    apiFetch.mockImplementation(async (path: string) => bootstrapResponse(sessionOf(path)));
    const connection = new FakeConnection();
    connection.set({ type: 'wifi' });
    setConnection(connection);
    prefetch.prefetchChatSnapshots([], { pinned: 'ses_main' });
    connection.set({ type: 'cellular' });
    await runIdle();
    expect(apiFetch).not.toHaveBeenCalled();

    connection.set({ type: 'wifi' });
    await runIdle();
    await vi.waitFor(() => expect(requested()).toEqual(['ses_main']), SETTLE);
  });

  it('stores nothing when the reader changed while the request was out', async () => {
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await requestOut();
    store.setLocalSnapshotPrincipal('remote:bob');
    response.resolve(bootstrapResponse('ses_a', 'answered for the last reader'));
    await runIdle();
    expect(await titleOf('ses_a')).toBeNull();
  });

  it('writes the prefetched conversation into the local message store too, and forgets it there on a 404', async () => {
    const localStore = await import('./localStore');
    const message = {
      id: 'msg_1', session_id: 'ses_a', type: 'result', author: 'agent', text: 'prefetched', content: {}, metadata: {},
      created_at: '2026-11-05T00:00:01.000000Z', updated_at: '2026-11-05T00:00:01.000000Z', delivered_at: null,
    };
    const held = async () => ((await localStore.readLocalStoreWindow('ses_a'))?.messages ?? []).map((row) => row.text);
    apiFetch.mockImplementation(async () => Response.json({ ...(await bootstrapResponse('ses_a').json()), messages: [message] }));
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await vi.waitFor(async () => expect(await held()).toEqual(['prefetched']));

    // A refresh of the pinned conversation the server now refuses.
    apiFetch.mockImplementation(async () => Response.json({ error: 'Session not found' }, { status: 404 }));
    prefetch.prefetchChatSnapshots([], { pinned: 'ses_a' });
    await runIdle();
    await vi.waitFor(async () => expect(await held()).toEqual([]));
  });

  it('deletes a record stored meanwhile when the server answers 404', async () => {
    const response = deferred<Response>();
    apiFetch.mockReturnValue(response.promise);
    prefetch.prefetchChatSnapshots(['ses_a']);
    await runIdle();
    await requestOut();
    await storeAs('ses_a', 'stored meanwhile');
    response.resolve(Response.json({ error: 'Session not found' }, { status: 404 }));
    await vi.waitFor(async () => expect(await titleOf('ses_a')).toBeNull(), SETTLE);
  });
});
