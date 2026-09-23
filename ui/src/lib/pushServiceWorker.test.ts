import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

import { describe, expect, it, vi } from 'vitest';

describe('push service worker notification launches', () => {
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
    expect(cache.put).toHaveBeenCalledWith(
      'https://avibe.local/__avibe/web-push-launch',
      expect.any(Response),
    );
    expect(storedPayload).toMatchObject({ url: '/chat/session-3', createdAt: expect.any(Number) });
    expect(openWindow).toHaveBeenCalledWith('https://avibe.local/chat/session-3');
    expect(openedClient.postMessage).toHaveBeenCalledWith({
      type: 'vibe.notification-click',
      url: '/chat/session-3',
    });
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
