import { describe, expect, it } from 'vitest';

import { internalPwaLinkTarget, shouldBlockPwaLoopbackLink } from './pwaNavigation';

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

describe('internalPwaLinkTarget', () => {
  const current = 'https://alex-app.avibe.bot/chat/session-123';

  it('keeps private Show Pages inside the AppShell route', () => {
    expect(internalPwaLinkTarget('/show/ses_123/', current)).toEqual({
      path: '/apps/show/ses_123',
      navigation: 'spa',
    });
    expect(internalPwaLinkTarget('https://alex-app.avibe.bot/show/a%20b%2Fc/', current)).toEqual({
      path: '/apps/show/a%20b%2Fc',
      navigation: 'spa',
    });
  });

  it('preserves private Show Page query, fragment, and nested route state', () => {
    expect(internalPwaLinkTarget('/show/ses_123/?tab=flow#top', current)).toEqual({
      path: '/show/ses_123/?tab=flow#top',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('/show/ses_123/projects/alpha?mode=edit#node-2', current)).toEqual({
      path: '/show/ses_123/projects/alpha?mode=edit#node-2',
      navigation: 'document',
    });
  });

  it('keeps public Show Pages in context while preserving their server document', () => {
    expect(internalPwaLinkTarget('/p/share_123/?theme=dark#chart', current)).toEqual({
      path: '/p/share_123/?theme=dark#chart',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('/p/share_123/projects/alpha?theme=dark#chart', current)).toEqual({
      path: '/p/share_123/projects/alpha?theme=dark#chart',
      navigation: 'document',
    });
  });

  it('keeps canonical app routes on the SPA path', () => {
    expect(internalPwaLinkTarget('/chat/session-456?msg=latest#reply', current)).toEqual({
      path: '/chat/session-456?msg=latest#reply',
      navigation: 'spa',
    });
    expect(internalPwaLinkTarget('/admin/settings/models?source=custom', current)).toEqual({
      path: '/admin/settings/models?source=custom',
      navigation: 'spa',
    });
    expect(internalPwaLinkTarget('/admin/settings/memory#profile', current)).toEqual({
      path: '/admin/settings/memory#profile',
      navigation: 'spa',
    });
  });

  it('keeps every other same-origin destination in the current document', () => {
    expect(internalPwaLinkTarget('/api/files/report.pdf?download=0#page=2', current)).toEqual({
      path: '/api/files/report.pdf?download=0#page=2',
      navigation: 'document',
    });
    expect(internalPwaLinkTarget('/custom/help?topic=pwa#recovery', current)).toEqual({
      path: '/custom/help?topic=pwa#recovery',
      navigation: 'document',
    });
  });

  it('leaves external and non-http destinations to their existing handlers', () => {
    expect(internalPwaLinkTarget('https://github.com/avibe-bot/avibe', current)).toBeNull();
    expect(internalPwaLinkTarget('https://alex-app.avibe.bot:8443/help', current)).toBeNull();
    expect(internalPwaLinkTarget('mailto:hello@example.com', current)).toBeNull();
  });
});
