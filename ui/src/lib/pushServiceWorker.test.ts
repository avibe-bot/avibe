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
});
