import { describe, expect, it, vi } from 'vitest';

import {
  ShowPagesInventoryStore,
  type ShowPage,
  type ShowPagesInventoryApi,
} from './showPagesStore';

const page = (overrides: Partial<ShowPage> = {}): ShowPage => ({
  session_id: 'session-1',
  visibility: 'private',
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
  it('single-flights simultaneous consumers and owns one events subscription', async () => {
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
    releaseSecond();
    expect(disconnect).toHaveBeenCalledTimes(1);
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
