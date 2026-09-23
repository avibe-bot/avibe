import { describe, expect, it, vi } from 'vitest';

import {
  createPendingWebPushLaunchReader,
  createResumedWebPushLaunchReader,
  parsePendingWebPushLaunch,
  WEB_PUSH_LAUNCH_MAX_AGE_MS,
} from './webPushLaunch';

describe('pending web-push launches', () => {
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
    const entryUrl = 'https://avibe.local/__avibe/web-push-launch';
    const cache = {
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
    const entryUrl = 'https://avibe.local/__avibe/web-push-launch';
    let payload: { url: string; createdAt: number } | null = null;
    const cache = {
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
});
