// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import { syncFaviconBadge } from './faviconBadge';

describe('favicon unread badge', () => {
  it('uses the branded unread icon and restores the original icon at zero', () => {
    const targetDocument = document.implementation.createHTMLDocument('favicon');
    const link = targetDocument.createElement('link');
    link.rel = 'icon';
    link.href = '/logo.png';
    targetDocument.head.appendChild(link);

    syncFaviconBadge(3, targetDocument);
    expect(link.getAttribute('href')).toBe('/logo-unread.png');
    syncFaviconBadge(0, targetDocument);
    expect(link.getAttribute('href')).toBe('/logo.png');
  });

  it('creates a PNG favicon when the document does not declare one', () => {
    const targetDocument = document.implementation.createHTMLDocument('favicon');

    syncFaviconBadge(1, targetDocument);

    const link = targetDocument.querySelector<HTMLLinkElement>('link[rel="icon"]');
    expect(link?.type).toBe('image/png');
    expect(link?.getAttribute('href')).toBe('/logo-unread.png');
    expect(link?.getAttribute('data-avibe-base-href')).toBe('/logo.png');
  });
});
