import { describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '../context/ApiContext';
import {
  commitShowPagesInventoryStore,
  getShowPagesInventoryStore,
  ShowPagesInventoryStore,
  type ShowPage,
  type ShowPagesInventoryApi,
} from './showPagesStore';

const page = (overrides: Partial<ShowPage> = {}): ShowPage => ({
  session_id: 'session-1',
  visibility: 'private',
  access_mode: 'private',
  access_revision: 0,
  can_manage: true,
  can_publish_public: true,
  title: 'Dashboard',
  platform: null,
  agent: null,
  path: '/tmp/show',
  icon_version: 'icon-v1',
  active_url: '/show/session-1/',
  private_url: '/show/session-1/',
  public_url: null,
  url_available: true,
  share_id: null,
  offline: false,
  offline_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

type EventHandlers = Parameters<ShowPagesInventoryApi['connectWorkbenchEvents']>[0];

describe('ShowPagesInventoryStore', () => {
  it('single-flights simultaneous consumers and keeps one events subscription', async () => {
    const response = deferred<{ pages: ShowPage[] }>();
    const disconnect = vi.fn();
    const api: ShowPagesInventoryApi = {
      getShowPages: vi.fn(() => response.promise),
      connectWorkbenchEvents: vi.fn(() => disconnect),
    };
    const store = new ShowPagesInventoryStore(api);

    const releaseFirst = store.activate();
    const releaseSecond = store.activate();
    const firstFlight = store.reload();

    expect(api.getShowPages).toHaveBeenCalledTimes(1);
    expect(api.connectWorkbenchEvents).toHaveBeenCalledTimes(1);
    expect(store.reload()).toBe(firstFlight);

    response.resolve({ pages: [page()] });
    await firstFlight;
    expect(store.getSnapshot().pages).toHaveLength(1);

    releaseFirst();
    expect(disconnect).not.toHaveBeenCalled();
    // The last consumer leaving does not end the subscription: the snapshot it
    // leaves behind is what an authorization change has to be able to reach.
    releaseSecond();
    expect(disconnect).not.toHaveBeenCalled();
    expect(api.connectWorkbenchEvents).toHaveBeenCalledTimes(1);
  });

  it('drops a retained snapshot when access changes with nothing reading it', async () => {
    let handlers: EventHandlers | undefined;
    const disconnect = vi.fn();
    const revalidation = deferred<{ pages: ShowPage[] }>();
    const getShowPages = vi
      .fn()
      .mockResolvedValueOnce({ pages: [page({ share_id: 'share-1' })] })
      .mockImplementationOnce(() => revalidation.promise);
    const store = new ShowPagesInventoryStore({
      getShowPages,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return disconnect;
      }),
    });

    const close = store.activate();
    await store.reload();
    handlers?.onConnected?.({ sub_id: 1, source: 'browser' });
    expect(store.getSnapshot().pages).toHaveLength(1);
    close();

    // Still subscribed, but invalidation-only: a reconnect or a session event
    // with nobody reading must not put a request on a route that renders none.
    handlers?.onConnected?.({ sub_id: 2, source: 'browser' });
    handlers?.onSessionActivity?.({
      session_id: 'session-1',
      scope_id: null,
      event: 'show_event',
    });
    await Promise.resolve();
    expect(getShowPages).toHaveBeenCalledTimes(1);

    handlers?.onAuthorizationChanged?.({
      project_ids: [],
      resource_kinds: ['show_page'],
    });

    // Asserted before any re-read resolves: the property must hold for a
    // revalidation that is slow, failing, or never issued at all, because the
    // next consumer renders this snapshot synchronously on its first frame.
    expect(store.getSnapshot().pages).toEqual([]);
    expect(store.getSnapshot().loaded).toBe(false);
    // Nothing left to protect and nobody reading, so the subscription ends too.
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(getShowPages).toHaveBeenCalledTimes(1);

    const reopened = store.activate();
    expect(store.getSnapshot().pages).toEqual([]);
    revalidation.resolve({ pages: [] });
    await store.reload();
    expect(getShowPages).toHaveBeenCalledTimes(2);
    reopened();
  });

  it('fences a read already in flight when access changes with nothing reading it', async () => {
    let handlers: EventHandlers | undefined;
    const stale = deferred<{ pages: ShowPage[] }>();
    const getShowPages = vi
      .fn()
      .mockImplementationOnce(() => stale.promise)
      .mockResolvedValueOnce({ pages: [] });
    const store = new ShowPagesInventoryStore({
      getShowPages,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    });

    const close = store.activate();
    const flight = store.reload();
    close();
    handlers?.onAuthorizationChanged?.({
      project_ids: [],
      resource_kinds: ['show_page'],
    });

    // The response left the server before the change, so it cannot repopulate
    // what was just dropped; the same single-flight promise reconciles instead.
    stale.resolve({ pages: [page({ share_id: 'share-1' })] });
    await flight;
    expect(getShowPages).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().pages).toEqual([]);
  });

  it('leaves one live subscription however often the API identity changes', async () => {
    let connected = 0;
    let disconnected = 0;
    const identityCount = 3;
    const identities = Array.from({ length: identityCount }, () => ({
      getShowPages: vi.fn().mockResolvedValue({ pages: [page()] }),
      connectWorkbenchEvents: vi.fn(() => {
        connected += 1;
        return () => {
          disconnected += 1;
        };
      }),
    }) as unknown as ApiContextType);

    // Each identity gets a consumer that reads, retains a snapshot, and leaves:
    // exactly the state the invalidation-only subscription is kept alive for, and
    // therefore the state in which a superseded store would strand one.
    for (const api of identities) {
      const store = getShowPagesInventoryStore(api);
      commitShowPagesInventoryStore(api);
      const close = store.activate();
      await store.reload();
      expect(store.getSnapshot().pages).toHaveLength(1);
      close();
    }

    // The count is the property: whatever the number of switches, one document
    // watches once. Asserted as a difference so it cannot be satisfied by never
    // subscribing in the first place.
    expect(connected).toBe(identityCount);
    expect(connected - disconnected).toBe(1);

    // ...and it is the current one that survives, still loaded and still able to
    // serve the snapshot its consumers render synchronously.
    const live = getShowPagesInventoryStore(identities[identityCount - 1]);
    expect(live.getSnapshot().pages).toHaveLength(1);
    const reopened = live.activate();
    await live.reload();
    expect(live.getSnapshot().pages).toHaveLength(1);
    reopened();
  });

  it('abandons the read a retired store had in flight', async () => {
    const stale = deferred<{ pages: ShowPage[] }>();
    const disconnect = vi.fn();
    const connectWorkbenchEvents = vi.fn(() => disconnect);
    const store = new ShowPagesInventoryStore({
      getShowPages: vi.fn(() => stale.promise),
      connectWorkbenchEvents,
    });

    const close = store.activate();
    const flight = store.reload();
    close();
    store.retire();
    expect(disconnect).toHaveBeenCalledTimes(1);

    // Retirement fences the read with the same revision bump a mutation uses, so
    // the response must be abandoned rather than retried: repopulating would
    // restore a snapshot nothing can render, and the settling read would
    // resubscribe on its way out.
    stale.resolve({ pages: [page()] });
    await flight;
    expect(store.getSnapshot().pages).toEqual([]);
    expect(connectWorkbenchEvents).toHaveBeenCalledTimes(1);
  });

  it('does not refetch when the initial events connection arrives after the activation read', async () => {
    let handlers: EventHandlers | undefined;
    const getShowPages = vi.fn().mockResolvedValue({ pages: [page()] });
    const store = new ShowPagesInventoryStore({
      getShowPages,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    });

    const release = store.activate();
    await store.reload();
    handlers?.onConnected?.({ sub_id: 1, source: 'browser' });
    await Promise.resolve();
    expect(getShowPages).toHaveBeenCalledTimes(1);

    handlers?.onConnected?.({ sub_id: 2, source: 'browser' });
    await store.reload();
    expect(getShowPages).toHaveBeenCalledTimes(2);
    release();
  });

  it('withdraws a retained page immediately when access is lost', async () => {
    const store = new ShowPagesInventoryStore({
      getShowPages: vi.fn().mockResolvedValue({ pages: [page()] }),
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    });

    await store.reload();
    expect(store.getSnapshot().pages).toHaveLength(1);

    store.removePage('session-1');
    expect(store.getSnapshot().pages).toEqual([]);
  });

  it('serves stale icon metadata immediately on reopen and revalidates in the background', async () => {
    const initial = deferred<{ pages: ShowPage[] }>();
    const refresh = deferred<{ pages: ShowPage[] }>();
    const getShowPages = vi
      .fn()
      .mockImplementationOnce(() => initial.promise)
      .mockImplementationOnce(() => refresh.promise);
    const api: ShowPagesInventoryApi = {
      getShowPages,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    };
    const store = new ShowPagesInventoryStore(api);

    const close = store.activate();
    const initialFlight = store.reload();
    initial.resolve({ pages: [page({ icon_version: 'cached-icon' })] });
    await initialFlight;
    close();

    const closeReopened = store.activate();
    const reopened = store.getSnapshot();
    expect(reopened.pages[0].icon_version).toBe('cached-icon');
    expect(reopened.loaded).toBe(true);
    expect(reopened.loading).toBe(true);
    expect(getShowPages).toHaveBeenCalledTimes(2);

    const refreshFlight = store.reload();
    refresh.resolve({ pages: [page({ icon_version: 'fresh-icon' })] });
    await refreshFlight;
    expect(store.getSnapshot().pages[0].icon_version).toBe('fresh-icon');
    closeReopened();
  });

  it('fans title, archive, and show events out to every subscriber', async () => {
    let handlers: EventHandlers | undefined;
    const getShowPages = vi
      .fn()
      .mockResolvedValueOnce({ pages: [page()] })
      .mockResolvedValueOnce({ pages: [] });
    const api: ShowPagesInventoryApi = {
      getShowPages,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    };
    const store = new ShowPagesInventoryStore(api);
    const firstConsumer = vi.fn();
    const secondConsumer = vi.fn();
    store.subscribe(firstConsumer);
    store.subscribe(secondConsumer);
    const release = store.activate();
    await store.reload();
    firstConsumer.mockClear();
    secondConsumer.mockClear();

    handlers?.onSessionActivity?.({
      session_id: 'session-1',
      scope_id: null,
      event: 'updated',
      title: 'Renamed',
    });
    expect(store.getSnapshot().pages[0].title).toBe('Renamed');
    expect(firstConsumer).toHaveBeenCalledTimes(1);
    expect(secondConsumer).toHaveBeenCalledTimes(1);

    handlers?.onSessionActivity?.({
      session_id: 'session-1',
      scope_id: null,
      event: 'archived',
    });
    expect(store.getSnapshot().pages).toEqual([]);
    expect(firstConsumer).toHaveBeenCalledTimes(2);
    expect(secondConsumer).toHaveBeenCalledTimes(2);

    handlers?.onSessionActivity?.({
      session_id: 'session-2',
      scope_id: null,
      event: 'show_event',
    });
    await store.reload();
    expect(getShowPages).toHaveBeenCalledTimes(2);
    release();
  });

  it('queues a show-event reconcile behind an older in-flight read', async () => {
    let handlers: EventHandlers | undefined;
    const stale = deferred<{ pages: ShowPage[] }>();
    const getShowPages = vi
      .fn()
      .mockImplementationOnce(() => stale.promise)
      .mockResolvedValueOnce({ pages: [page({ session_id: 'session-2' })] });
    const store = new ShowPagesInventoryStore({
      getShowPages,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    });

    const release = store.activate();
    const flight = store.reload();
    handlers?.onSessionActivity?.({
      session_id: 'session-2',
      scope_id: null,
      event: 'show_event',
    });
    expect(store.reload()).toBe(flight);

    stale.resolve({ pages: [] });
    await flight;
    expect(getShowPages).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().pages[0].session_id).toBe('session-2');
    release();
  });

  it('revalidates and removes revoked pages after authorization changes', async () => {
    let handlers: EventHandlers | undefined;
    const getShowPages = vi
      .fn()
      .mockResolvedValueOnce({ pages: [page()] })
      .mockResolvedValueOnce({ pages: [] });
    const store = new ShowPagesInventoryStore({
      getShowPages,
      connectWorkbenchEvents: vi.fn((next) => {
        handlers = next;
        return vi.fn();
      }),
    });

    const release = store.activate();
    await store.reload();
    expect(store.getSnapshot().pages).toHaveLength(1);

    handlers?.onAuthorizationChanged?.({
      project_ids: [],
      resource_kinds: ['show_page'],
    });
    await store.reload();

    expect(getShowPages).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().pages).toEqual([]);
    release();
  });

  it('reconciles after a mutation instead of letting an older read overwrite it', async () => {
    const stale = deferred<{ pages: ShowPage[] }>();
    const reconciled = deferred<{ pages: ShowPage[] }>();
    const getShowPages = vi
      .fn()
      .mockResolvedValueOnce({ pages: [page()] })
      .mockImplementationOnce(() => stale.promise)
      .mockImplementationOnce(() => reconciled.promise);
    const store = new ShowPagesInventoryStore({
      getShowPages,
      connectWorkbenchEvents: vi.fn(() => vi.fn()),
    });

    const release = store.activate();
    await store.reload();
    const refreshFlight = store.reload();
    store.mergePage({ session_id: 'session-1', visibility: 'public' });
    expect(store.getSnapshot().pages[0].visibility).toBe('public');

    stale.resolve({ pages: [page({ visibility: 'private' })] });
    await Promise.resolve();
    expect(getShowPages).toHaveBeenCalledTimes(3);

    reconciled.resolve({ pages: [page({ visibility: 'public' })] });
    await refreshFlight;
    expect(store.getSnapshot().pages[0].visibility).toBe('public');
    release();
  });
});
