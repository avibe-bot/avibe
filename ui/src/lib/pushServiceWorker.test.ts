import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

import { describe, expect, it, vi } from 'vitest';

describe('push service worker notification launches', () => {
  it('hands a tapped session to a reused app window without hard navigation', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/settings/general',
      focus: vi.fn(async () => client),
      navigate: vi.fn(async (href: string) => ({ url: href })),
      postMessage: vi.fn(),
    };
    const openWindow = vi.fn();
    let storedPayload: unknown;
    const cache = {
      put: vi.fn(async (_request: string, response: Response) => {
        storedPayload = await response.json();
      }),
    };
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn(async () => cache) },
      clients: { matchAll: vi.fn(async () => [client]), openWindow },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, { self: worker, navigator: {}, URL, Response, Date, Number, JSON, Promise });

    let completion: Promise<unknown> | undefined;
    handlers.get('notificationclick')?.({
      notification: { close: vi.fn(), data: { url: '/chat/session-3' } },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(client.focus).toHaveBeenCalledOnce();
    expect(storedPayload).toMatchObject({ url: '/chat/session-3', createdAt: expect.any(Number) });
    expect(cache.put.mock.invocationCallOrder[0]).toBeLessThan(client.focus.mock.invocationCallOrder[0]);
    expect(client.navigate).not.toHaveBeenCalled();
    expect(client.postMessage).toHaveBeenCalledWith({
      type: 'vibe.notification-click',
      url: '/chat/session-3',
    });
    expect(openWindow).not.toHaveBeenCalled();
  });

  it('delivers rapid clicks in tap order even when the first focus is delayed', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    let finishFirstFocus: (client: unknown) => void = () => {};
    const firstFocus = new Promise<unknown>((resolve) => {
      finishFirstFocus = resolve;
    });
    const client = {
      url: 'https://avibe.local/settings/general',
      focus: vi.fn().mockImplementationOnce(() => firstFocus).mockImplementationOnce(async () => client),
      postMessage: vi.fn(),
    };
    const cache = { put: vi.fn(async () => {}) };
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn(async () => cache) },
      clients: { matchAll: vi.fn(async () => [client]) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, { self: worker, navigator: {}, URL, Response, Date, Number, JSON, Promise });

    const click = (url: string) => {
      let completion: Promise<unknown> | undefined;
      handlers.get('notificationclick')?.({
        notification: { close: vi.fn(), data: { url } },
        waitUntil: (promise: Promise<unknown>) => { completion = promise; },
      });
      return completion;
    };
    const first = click('/chat/session-1');
    await vi.waitFor(() => expect(client.focus).toHaveBeenCalledTimes(1));
    const second = click('/chat/session-2');
    expect(client.postMessage).not.toHaveBeenCalled();

    finishFirstFocus(client);
    await Promise.all([first, second]);
    expect(client.postMessage.mock.calls.map(([message]) => message.url)).toEqual([
      '/chat/session-1',
      '/chat/session-2',
    ]);
  });

  it('posts to a reused window without WindowClient.navigate support', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/inbox',
      focus: vi.fn(async () => client),
      navigate: vi.fn(async () => null),
      postMessage: vi.fn(),
    };
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => [client]) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, { self: worker, navigator: {}, URL, Response, Date, Number, JSON, Promise });

    let completion: Promise<unknown> | undefined;
    handlers.get('notificationclick')?.({
      notification: { close: vi.fn(), data: { url: '/chat/session-4' } },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(client.postMessage).toHaveBeenCalledWith({
      type: 'vibe.notification-click',
      url: '/chat/session-4',
    });
  });

  it('opens the target when an existing app window cannot be focused', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/apps/files',
      focus: vi.fn(async () => { throw new Error('window closed'); }),
    };
    const openWindow = vi.fn(async () => null);
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => [client]), openWindow },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, { self: worker, navigator: {}, URL, Response, Date, Number, JSON, Promise });

    let completion: Promise<unknown> | undefined;
    handlers.get('notificationclick')?.({
      notification: { close: vi.fn(), data: { url: '/chat/session-5' } },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(openWindow).toHaveBeenCalledWith('https://avibe.local/chat/session-5');
  });

  it('persists and posts the target when no app-shell client is open', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    let storedPayload: unknown;
    const cache = {
      put: vi.fn(async (_request: string, response: Response) => {
        storedPayload = await response.json();
      }),
    };
    const openedClient = { postMessage: vi.fn() };
    const openWindow = vi.fn(async () => openedClient);
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn(async () => cache) },
      clients: { matchAll: vi.fn(async () => []), openWindow },
      registration: { showNotification: vi.fn() },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };

    runInNewContext(source, { self: worker, navigator: {}, URL, Response, Date, Number, JSON, Promise });

    let completion: Promise<unknown> | undefined;
    const notification = {
      close: vi.fn(),
      data: { url: '/chat/session-3' },
    };
    handlers.get('notificationclick')?.({
      notification,
      waitUntil: (promise: Promise<unknown>) => {
        completion = promise;
      },
    });
    await completion;

    expect(notification.close).toHaveBeenCalledOnce();
    expect(cache.put.mock.calls[0][0]).toMatch(
      /^https:\/\/avibe\.local\/__avibe\/web-push-launch\/\d+-[a-z0-9]+$/,
    );
    expect(storedPayload).toMatchObject({ url: '/chat/session-3', createdAt: expect.any(Number) });
    expect(openWindow).toHaveBeenCalledWith('https://avibe.local/chat/session-3');
    expect(openedClient.postMessage).toHaveBeenCalledWith({
      type: 'vibe.notification-click',
      url: '/chat/session-3',
    });
  });

  it('uses current server unread count instead of a delayed push count for the app badge', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const setAppBadge = vi.fn(async () => {});
    const clearAppBadge = vi.fn(async () => {});
    const fetchMock = vi.fn(async () => Response.json({ unread_total: 0 }));
    const showNotification = vi.fn(async () => {});
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => []) },
      registration: { showNotification },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: { setAppBadge, clearAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ title: 'Avibe', badge_count: 3 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(showNotification).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith('/api/inbox?platform=avibe&limit=1', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Avibe-Background-Push': '1' },
    });
    expect(clearAppBadge).toHaveBeenCalledOnce();
    expect(setAppBadge).not.toHaveBeenCalled();
  });

  it('leaves the badge alone when the current unread count cannot be verified', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const setAppBadge = vi.fn(async () => {});
    const clearAppBadge = vi.fn(async () => {});
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => []) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: vi.fn(async () => new Response(null, { status: 401 })),
      navigator: { setAppBadge, clearAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 3 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(setAppBadge).not.toHaveBeenCalled();
    expect(clearAppBadge).not.toHaveBeenCalled();
  });

  it('sets a verified nonzero badge count while the app is closed', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const setAppBadge = vi.fn(async () => {});
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => []) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: vi.fn(async () => Response.json({ unread_total: 2 })),
      navigator: { setAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 1 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(setAppBadge).toHaveBeenCalledWith(2);
  });

  it('lets a visible app refresh its own badge instead of writing from the worker', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/chat/session-1',
      visibilityState: 'visible',
      postMessage: vi.fn(),
    };
    const setAppBadge = vi.fn(async () => {});
    const fetchMock = vi.fn();
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => [client]) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: { setAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 2 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(fetchMock).not.toHaveBeenCalled();
    expect(setAppBadge).not.toHaveBeenCalled();
    expect(client.postMessage).toHaveBeenCalledWith({ type: 'vibe.push-badge-refresh' });
  });

  it('does not ask a hidden page to make an interactive Inbox read after Push', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/inbox',
      visibilityState: 'hidden',
      postMessage: vi.fn(),
    };
    const setAppBadge = vi.fn(async () => {});
    const fetchMock = vi.fn(async () => Response.json({ unread_total: 2 }));
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => [client]) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: { setAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 1 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(fetchMock).toHaveBeenCalledWith('/api/inbox?platform=avibe&limit=1', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Avibe-Background-Push': '1' },
    });
    expect(setAppBadge).toHaveBeenCalledWith(2);
    expect(client.postMessage).not.toHaveBeenCalled();
  });

  it('notifies a page that becomes visible while the worker writes a badge', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/inbox',
      visibilityState: 'hidden',
      postMessage: vi.fn(),
    };
    const setAppBadge = vi.fn(async () => {
      client.visibilityState = 'visible';
    });
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => [client]) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: vi.fn(async () => Response.json({ unread_total: 2 })),
      navigator: { setAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 1 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await completion;

    expect(setAppBadge).toHaveBeenCalledWith(2);
    expect(client.postMessage).toHaveBeenCalledWith({ type: 'vibe.push-badge-refresh' });
  });

  it('does not apply a fetched count after the app becomes visible', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const client = {
      url: 'https://avibe.local/inbox',
      visibilityState: 'hidden',
      postMessage: vi.fn(),
    };
    const setAppBadge = vi.fn(async () => {});
    let releaseFetch: (response: Response) => void = () => {};
    const fetchPending = new Promise<Response>((resolve) => { releaseFetch = resolve; });
    let fetchStarted: () => void = () => {};
    const started = new Promise<void>((resolve) => { fetchStarted = resolve; });
    const fetchMock = vi.fn(() => {
      fetchStarted();
      return fetchPending;
    });
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => [client]) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: { setAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 3 }) },
      waitUntil: (promise: Promise<unknown>) => { completion = promise; },
    });
    await started;
    client.visibilityState = 'visible';
    releaseFetch(Response.json({ unread_total: 3 }));
    await completion;

    expect(setAppBadge).not.toHaveBeenCalled();
    expect(client.postMessage).toHaveBeenCalledWith({ type: 'vibe.push-badge-refresh' });
  });

  it('does not restore an older push count after the page clears the badge', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const setAppBadge = vi.fn(async () => {});
    const clearAppBadge = vi.fn(async () => {});
    let releaseFetch: (response: Response) => void = () => {};
    const fetchPending = new Promise<Response>((resolve) => { releaseFetch = resolve; });
    let fetchStarted: () => void = () => {};
    const started = new Promise<void>((resolve) => { fetchStarted = resolve; });
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => []) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: vi.fn(() => { fetchStarted(); return fetchPending; }),
      navigator: { setAppBadge, clearAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let pushCompletion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 3 }) },
      waitUntil: (promise: Promise<unknown>) => { pushCompletion = promise; },
    });
    await started;
    let pageCompletion: Promise<unknown> | undefined;
    handlers.get('message')?.({
      data: { type: 'vibe.app-badge-current', count: 0 },
      waitUntil: (promise: Promise<unknown>) => { pageCompletion = promise; },
    });
    await pageCompletion;
    releaseFetch(Response.json({ unread_total: 3 }));
    await pushCompletion;

    expect(clearAppBadge).toHaveBeenCalledOnce();
    expect(setAppBadge).not.toHaveBeenCalled();
  });

  it('serializes a page clear after a worker badge write already started', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    let releaseSet: () => void = () => {};
    const setPending = new Promise<void>((resolve) => { releaseSet = resolve; });
    let setStarted: () => void = () => {};
    const started = new Promise<void>((resolve) => { setStarted = resolve; });
    const setAppBadge = vi.fn(() => { setStarted(); return setPending; });
    const clearAppBadge = vi.fn(async () => {});
    const worker = {
      location: { origin: 'https://avibe.local' },
      clients: { matchAll: vi.fn(async () => []) },
      registration: { showNotification: vi.fn(async () => {}) },
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    runInNewContext(source, {
      self: worker,
      fetch: vi.fn(async () => Response.json({ unread_total: 3 })),
      navigator: { setAppBadge, clearAppBadge },
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
    });

    let pushCompletion: Promise<unknown> | undefined;
    handlers.get('push')?.({
      data: { json: () => ({ badge_count: 3 }) },
      waitUntil: (promise: Promise<unknown>) => { pushCompletion = promise; },
    });
    await started;
    let pageCompletion: Promise<unknown> | undefined;
    handlers.get('message')?.({
      data: { type: 'vibe.app-badge-current', count: 0 },
      waitUntil: (promise: Promise<unknown>) => { pageCompletion = promise; },
    });
    releaseSet();
    await Promise.all([pushCompletion, pageCompletion]);

    expect(setAppBadge).toHaveBeenCalledWith(3);
    expect(clearAppBadge).toHaveBeenCalledOnce();
    expect(setAppBadge.mock.invocationCallOrder[0]).toBeLessThan(clearAppBadge.mock.invocationCallOrder[0]);
  });

  it('syncs a replacement subscription and retires the old endpoint', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    const newSubscription = {
      endpoint: 'https://push.example.test/sub/new',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      toJSON: () => ({
        endpoint: 'https://push.example.test/sub/new',
        keys: { p256dh: 'new-key', auth: 'new-auth' },
      }),
    };
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn() },
      clients: { matchAll: vi.fn(), openWindow: vi.fn() },
      registration: { showNotification: vi.fn(), pushManager: { subscribe: vi.fn() } },
      atob,
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      requests.push({ input, init });
      if (input === '/api/csrf-token') {
        return { ok: true, json: async () => ({ csrf_token: 'csrf-token' }) };
      }
      if (input === '/api/web-push/vapid-public-key') {
        return { ok: true, json: async () => ({ public_key: 'AQIDBA' }) };
      }
      return { ok: true, json: async () => ({}) };
    });

    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: {},
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
      Uint8Array,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('pushsubscriptionchange')?.({
      newSubscription,
      oldSubscription: { endpoint: 'https://push.example.test/sub/old' },
      waitUntil: (promise: Promise<unknown>) => {
        completion = promise;
      },
    });
    await completion;

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(requests[0].input).toBe('/api/web-push/vapid-public-key');
    expect(requests[1].input).toBe('/api/csrf-token');
    expect(requests[2]).toMatchObject({
      input: '/api/web-push/subscriptions',
      init: {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-Vibe-CSRF-Token': 'csrf-token',
        },
      },
    });
    expect(JSON.parse(String(requests[2].init?.body))).toEqual({
      subscription: {
        endpoint: 'https://push.example.test/sub/new',
        keys: { p256dh: 'new-key', auth: 'new-auth' },
      },
      previous_endpoints: ['https://push.example.test/sub/old'],
      background_rotation: true,
    });
  });

  it('uses the last confirmed endpoint when the rotation event has no old subscription', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const oldEndpoint = 'https://push.example.test/sub/old';
    const newEndpoint = 'https://push.example.test/sub/new';
    const cache = {
      match: vi.fn(async () => Response.json({ endpoint: oldEndpoint })),
      put: vi.fn(async () => undefined),
    };
    const subscription = {
      endpoint: newEndpoint,
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      toJSON: () => ({
        endpoint: newEndpoint,
        keys: { p256dh: 'new-key', auth: 'new-auth' },
      }),
    };
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn(async () => cache) },
      registration: { pushManager: { subscribe: vi.fn() } },
      atob,
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    const fetchMock = vi.fn(async (input: string, _init?: RequestInit) => {
      if (input === '/api/web-push/vapid-public-key') {
        return Response.json({ public_key: 'AQIDBA' });
      }
      if (input === '/api/csrf-token') {
        return Response.json({ csrf_token: 'csrf-token' });
      }
      return Response.json({ accepted: true });
    });
    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: {},
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
      Uint8Array,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('pushsubscriptionchange')?.({
      newSubscription: subscription,
      oldSubscription: null,
      waitUntil: (promise: Promise<unknown>) => {
        completion = promise;
      },
    });
    await completion;

    expect(cache.match).toHaveBeenCalledWith('https://avibe.local/__avibe/web-push-endpoint');
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toMatchObject({
      previous_endpoints: [oldEndpoint],
      background_rotation: true,
    });
    expect(cache.put).toHaveBeenCalledWith(
      'https://avibe.local/__avibe/web-push-endpoint',
      expect.any(Response),
    );
    expect(await cache.put.mock.calls[0][1].json()).toEqual({ endpoint: newEndpoint });
  });

  it('replaces a browser-provided subscription when its VAPID key is stale', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    const staleSubscription = {
      endpoint: 'https://push.example.test/sub/stale',
      options: { applicationServerKey: new Uint8Array([9, 9, 9, 9]).buffer },
      unsubscribe: vi.fn(async () => true),
      toJSON: () => ({
        endpoint: 'https://push.example.test/sub/stale',
        keys: { p256dh: 'stale-key', auth: 'stale-auth' },
      }),
    };
    const currentSubscription = {
      endpoint: 'https://push.example.test/sub/current',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      toJSON: () => ({
        endpoint: 'https://push.example.test/sub/current',
        keys: { p256dh: 'current-key', auth: 'current-auth' },
      }),
    };
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn() },
      clients: { matchAll: vi.fn(), openWindow: vi.fn() },
      registration: {
        showNotification: vi.fn(),
        pushManager: {
          getSubscription: vi.fn(async () => null),
          subscribe: vi.fn(async () => currentSubscription),
        },
      },
      atob,
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      requests.push({ input, init });
      if (input === '/api/web-push/vapid-public-key') {
        return { ok: true, json: async () => ({ public_key: 'AQIDBA' }) };
      }
      if (input === '/api/csrf-token') {
        return { ok: true, json: async () => ({ csrf_token: 'csrf-token' }) };
      }
      return { ok: true, json: async () => ({}) };
    });

    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: {},
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
      Uint8Array,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('pushsubscriptionchange')?.({
      newSubscription: staleSubscription,
      oldSubscription: { endpoint: 'https://push.example.test/sub/old' },
      waitUntil: (promise: Promise<unknown>) => {
        completion = promise;
      },
    });
    await completion;

    expect(staleSubscription.unsubscribe).toHaveBeenCalledOnce();
    expect(worker.registration.pushManager.subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: new Uint8Array([1, 2, 3, 4]),
    });
    expect(JSON.parse(String(requests[2].init?.body))).toEqual({
      subscription: currentSubscription.toJSON(),
      previous_endpoints: ['https://push.example.test/sub/old'],
      background_rotation: true,
    });
  });

  it('does not sync a stale subscription the browser refused to remove', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const staleSubscription = {
      endpoint: 'https://push.example.test/sub/stale',
      options: { applicationServerKey: new Uint8Array([9, 9, 9, 9]).buffer },
      unsubscribe: vi.fn(async () => false),
    };
    const pushManager = {
      getSubscription: vi.fn(async () => staleSubscription),
      subscribe: vi.fn(),
    };
    const worker = {
      location: { origin: 'https://avibe.local' },
      registration: { pushManager },
      atob,
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    const fetchMock = vi.fn(async () => Response.json({ public_key: 'AQIDBA' }));
    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: {},
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
      Uint8Array,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('pushsubscriptionchange')?.({
      newSubscription: staleSubscription,
      oldSubscription: { endpoint: 'https://push.example.test/sub/old' },
      waitUntil: (promise: Promise<unknown>) => {
        completion = promise;
      },
    });
    await completion;

    expect(staleSubscription.unsubscribe).toHaveBeenCalledOnce();
    expect(pushManager.getSubscription).toHaveBeenCalledOnce();
    expect(pushManager.subscribe).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('retries subscription sync once after an invalid CSRF token', async () => {
    const source = await readFile(new URL('../../public/push-sw.js', import.meta.url), 'utf8');
    const handlers = new Map<string, (event: unknown) => void>();
    const requests: Array<{ input: string; init?: RequestInit }> = [];
    const subscription = {
      endpoint: 'https://push.example.test/sub/retry',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      toJSON: () => ({
        endpoint: 'https://push.example.test/sub/retry',
        keys: { p256dh: 'retry-key', auth: 'retry-auth' },
      }),
    };
    let csrfRequests = 0;
    let subscriptionPosts = 0;
    const worker = {
      location: { origin: 'https://avibe.local' },
      caches: { open: vi.fn() },
      clients: { matchAll: vi.fn(), openWindow: vi.fn() },
      registration: { showNotification: vi.fn(), pushManager: { subscribe: vi.fn() } },
      atob,
      addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler),
    };
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      requests.push({ input, init });
      if (input === '/api/web-push/vapid-public-key') {
        return Response.json({ public_key: 'AQIDBA' });
      }
      if (input === '/api/csrf-token') {
        csrfRequests += 1;
        return Response.json({ csrf_token: csrfRequests === 1 ? 'stale-token' : 'fresh-token' });
      }
      if (input === '/api/web-push/subscriptions') {
        subscriptionPosts += 1;
        if (subscriptionPosts === 1) {
          return Response.json(
            { ok: false, message: 'Forbidden: invalid csrf token' },
            { status: 403 },
          );
        }
      }
      return Response.json({ ok: true });
    });

    runInNewContext(source, {
      self: worker,
      fetch: fetchMock,
      navigator: {},
      URL,
      Response,
      Date,
      Number,
      JSON,
      Promise,
      Uint8Array,
    });

    let completion: Promise<unknown> | undefined;
    handlers.get('pushsubscriptionchange')?.({
      newSubscription: subscription,
      oldSubscription: { endpoint: 'https://push.example.test/sub/old' },
      waitUntil: (promise: Promise<unknown>) => {
        completion = promise;
      },
    });
    await completion;

    expect(csrfRequests).toBe(2);
    expect(subscriptionPosts).toBe(2);
    expect(new Headers(requests[2].init?.headers).get('X-Vibe-CSRF-Token')).toBe('stale-token');
    expect(new Headers(requests[4].init?.headers).get('X-Vibe-CSRF-Token')).toBe('fresh-token');
  });
});
