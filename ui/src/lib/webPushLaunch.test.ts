import { describe, expect, it, vi } from 'vitest';

import {
  createPendingWebPushLaunchReader,
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
});
