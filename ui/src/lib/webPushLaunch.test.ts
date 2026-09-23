import { describe, expect, it, vi } from 'vitest';

import {
  createPendingWebPushLaunchReader,
  createResumedWebPushLaunchReader,
  createWebPushLaunchNavigation,
  parsePendingWebPushLaunch,
  WEB_PUSH_LAUNCH_MAX_AGE_MS,
} from './webPushLaunch';

describe('pending web-push launches', () => {
  it('does not let a delayed resume read override a later click message', async () => {
    let finishResume: (path: string | null) => void = () => {};
    const resume = new Promise<string | null>((resolve) => {
      finishResume = resolve;
    });
    const read = vi.fn((expectedPath?: string) => expectedPath ? Promise.resolve(null) : resume);
    const navigate = vi.fn();
    const navigation = createWebPushLaunchNavigation(read, navigate);

    navigation.reactivated();
    navigation.notificationClick('/chat/session-2');
    finishResume('/chat/session-1');
    await resume;
    await Promise.resolve();

    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/chat/session-2');
    expect(read).toHaveBeenCalledWith('/chat/session-2');
    navigation.dispose();
  });

  it('keeps only the newest of two overlapping resume reads', async () => {
    const finish: Array<(path: string | null) => void> = [];
    const read = vi.fn(() => new Promise<string | null>((resolve) => finish.push(resolve)));
    const navigate = vi.fn();
    const navigation = createWebPushLaunchNavigation(read, navigate);

    navigation.reactivated();
    navigation.reactivated();
    finish[1]('/chat/session-2');
    await Promise.resolve();
    finish[0]('/chat/session-1');
    await Promise.resolve();

    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/chat/session-2');
    navigation.dispose();
  });

  it('accepts only fresh canonical app paths', () => {
    const now = 1_000_000;
    expect(parsePendingWebPushLaunch({ url: '/chat/session-1?from=push', createdAt: now - 1 }, now)).toBe(
      '/chat/session-1',
    );
    expect(
      parsePendingWebPushLaunch(
        { url: '/chat/session-1', createdAt: now - WEB_PUSH_LAUNCH_MAX_AGE_MS - 1 },
        now,
      ),
    ).toBeNull();
    expect(parsePendingWebPushLaunch({ url: '//example.com/inbox', createdAt: now }, now)).toBeNull();
  });

  it('consumes the cached target once and shares the result across StrictMode reads', async () => {
    const now = 2_000_000;
    const entryUrl = 'https://avibe.local/__avibe/web-push-launch/click-1';
    const cache = {
      keys: vi.fn(async () => [entryUrl]),
      match: vi.fn(async () => new Response(JSON.stringify({ url: '/chat/session-2', createdAt: now }))),
      delete: vi.fn(async () => true),
    };
    const open = vi.fn(async () => cache);
    const read = createPendingWebPushLaunchReader(() => ({
      cacheStorage: { open },
      origin: 'https://avibe.local',
      now: () => now,
    }));

    await expect(Promise.all([read(), read()])).resolves.toEqual(['/chat/session-2', '/chat/session-2']);
    expect(open).toHaveBeenCalledOnce();
    expect(cache.match).toHaveBeenCalledWith(entryUrl);
    expect(cache.delete).toHaveBeenCalledWith(entryUrl);
  });

  it('falls back safely when Cache Storage is unavailable', async () => {
    const read = createPendingWebPushLaunchReader(() => null);
    await expect(read()).resolves.toBeNull();
  });

  it('reads a new target on resume and keeps a newer one when an older message arrives', async () => {
    const now = 2_000_000;
    const entryUrl = 'https://avibe.local/__avibe/web-push-launch/click-1';
    let payload: { url: string; createdAt: number } | null = null;
    const cache = {
      keys: vi.fn(async () => payload ? [entryUrl] : []),
      match: vi.fn(async () => payload ? Response.json(payload) : undefined),
      delete: vi.fn(async () => { payload = null; return true; }),
    };
    const read = createResumedWebPushLaunchReader(() => ({
      cacheStorage: { open: async () => cache },
      origin: 'https://avibe.local',
      now: () => now,
    }));

    payload = { url: '/chat/session-2', createdAt: now };
    await expect(read('/chat/session-1')).resolves.toBeNull();
    expect(cache.delete).not.toHaveBeenCalled();
    await expect(read()).resolves.toBe('/chat/session-2');
    expect(cache.delete).toHaveBeenCalledWith(entryUrl);

    payload = { url: '/chat/session-3', createdAt: now };
    await expect(read('/chat/session-3?from=push')).resolves.toBe('/chat/session-3');
    expect(cache.delete).toHaveBeenCalledTimes(2);
  });

  it('does not delete a second click stored while the first response is being read', async () => {
    const now = 2_000_000;
    const firstUrl = 'https://avibe.local/__avibe/web-push-launch/click-1';
    const secondUrl = 'https://avibe.local/__avibe/web-push-launch/click-2';
    const stored = new Map<string, { url: string; createdAt: number }>([
      [firstUrl, { url: '/chat/session-1', createdAt: now - 1 }],
    ]);
    let releaseFirst: (payload: { url: string; createdAt: number }) => void = () => {};
    let firstMatched: () => void = () => {};
    const firstBody = new Promise<{ url: string; createdAt: number }>((resolve) => {
      releaseFirst = resolve;
    });
    const matched = new Promise<void>((resolve) => {
      firstMatched = resolve;
    });
    const cache = {
      keys: vi.fn(async () => [...stored.keys()]),
      match: vi.fn(async (key: string) => {
        if (key === firstUrl) {
          firstMatched();
          return { json: () => firstBody } as Response;
        }
        const payload = stored.get(key);
        return payload ? Response.json(payload) : undefined;
      }),
      delete: vi.fn(async (key: string) => stored.delete(key)),
    };
    const read = createResumedWebPushLaunchReader(() => ({
      cacheStorage: { open: async () => cache },
      origin: 'https://avibe.local',
      now: () => now,
    }));

    const firstRead = read();
    await matched;
    stored.set(secondUrl, { url: '/chat/session-2', createdAt: now });
    releaseFirst({ url: '/chat/session-1', createdAt: now - 1 });
    await expect(firstRead).resolves.toBe('/chat/session-1');
    expect(stored.has(secondUrl)).toBe(true);
    expect(cache.delete).toHaveBeenCalledWith(firstUrl);
    expect(cache.delete).not.toHaveBeenCalledWith(secondUrl);

    await expect(read()).resolves.toBe('/chat/session-2');
    expect(stored.size).toBe(0);
  });

  it('takes the newest clicked session and drains older handoffs from the same snapshot', async () => {
    const now = 2_000_000;
    const firstUrl = 'https://avibe.local/__avibe/web-push-launch/click-1';
    const secondUrl = 'https://avibe.local/__avibe/web-push-launch/click-2';
    const stored = new Map<string, { url: string; createdAt: number }>([
      [secondUrl, { url: '/chat/session-2', createdAt: now }],
      [firstUrl, { url: '/chat/session-1', createdAt: now - 1 }],
    ]);
    const cache = {
      keys: vi.fn(async () => [...stored.keys()]),
      match: vi.fn(async (key: string) => Response.json(stored.get(key))),
      delete: vi.fn(async (key: string) => stored.delete(key)),
    };
    const read = createResumedWebPushLaunchReader(() => ({
      cacheStorage: { open: async () => cache },
      origin: 'https://avibe.local',
      now: () => now,
    }));

    await expect(read()).resolves.toBe('/chat/session-2');
    expect(stored.size).toBe(0);
    await expect(read()).resolves.toBeNull();
  });
});
