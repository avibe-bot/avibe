// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import { createFaviconBadgeDataUrl, formatFaviconBadgeCount, syncFaviconBadge } from './faviconBadge';

describe('favicon unread badge', () => {
  it('formats large counts compactly', () => {
    expect(formatFaviconBadgeCount(4)).toBe('4');
    expect(formatFaviconBadgeCount(100)).toBe('99+');
  });

  it('writes a self-contained badge and restores the original icon at zero', () => {
    const targetDocument = document.implementation.createHTMLDocument('favicon');
    const link = targetDocument.createElement('link');
    link.rel = 'icon';
    link.href = '/logo.png';
    targetDocument.head.appendChild(link);

    syncFaviconBadge(3, targetDocument);
    expect(link.href).toContain('data:image/svg+xml');
    expect(decodeURIComponent(link.href)).toContain('>3</text>');
    syncFaviconBadge(0, targetDocument);
    expect(link.getAttribute('href')).toBe('/logo.png');
    expect(createFaviconBadgeDataUrl(12)).toContain('data:image/svg+xml');
  });
});
