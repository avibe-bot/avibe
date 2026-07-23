import { describe, expect, it } from 'vitest';

import { shouldBlockPwaLoopbackLink } from './pwaNavigation';

describe('PWA navigation', () => {
  const remotePage = 'https://alex-app.avibe.bot/chat/session-123';

  it.each([
    'http://localhost:5123',
    'http://dev.localhost:5173/path',
    'http://127.0.0.1:15130/chat/session-456',
    'http://127.12.34.56/path',
    'http://[::1]:5123/path',
  ])('blocks a loopback target from a remote page: %s', (href) => {
    expect(shouldBlockPwaLoopbackLink(href, remotePage)).toBe(true);
  });

  it.each([
    '/chat/session-456',
    'https://github.com/avibe-bot/avibe',
    'https://192.168.1.20:5123',
    'mailto:hello@example.com',
    'not a url',
  ])('allows a non-loopback target: %s', (href) => {
    expect(shouldBlockPwaLoopbackLink(href, remotePage)).toBe(false);
  });

  it('allows loopback links when Avibe itself is open on loopback', () => {
    expect(
      shouldBlockPwaLoopbackLink(
        'http://127.0.0.1:15130/chat/session-456',
        'http://127.0.0.1:5123/chat/session-123',
      ),
    ).toBe(false);
  });
});
