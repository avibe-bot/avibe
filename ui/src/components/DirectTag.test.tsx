/** @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '../i18n';
import { setLiveStreamVia } from '../lib/liveStreamVia';
import type { NativeDirectPathKind } from '../lib/native';
import { DirectTag } from './DirectTag';

// The App's path as its status says; null stands for a browser, or a path not named.
const app = vi.hoisted(() => ({
  path: null as NativeDirectPathKind | null,
  listeners: new Set<() => void>(),
}));

vi.mock('../lib/native', () => ({
  nativeDirectPathKind: () => app.path,
  onNativeDirectPreference: (handler: () => void) => {
    app.listeners.add(handler);
    return () => app.listeners.delete(handler);
  },
}));

function appPath(next: NativeDirectPathKind | null) {
  act(() => {
    app.path = next;
    for (const listener of Array.from(app.listeners)) listener();
  });
}

// ui/design: mobile-app/direct-tag
afterEach(() => {
  cleanup();
  act(() => setLiveStreamVia(null));
  app.path = null;
});

describe('DirectTag', () => {
  it('says 直连 only for a direct address, 中继 through the relay, and nothing otherwise', async () => {
    await i18n.changeLanguage('zh');
    const { container } = render(<DirectTag />);
    expect(container.innerHTML).toBe('');

    // On the direct ingress, but the App names no path (a browser, or not yet known): nothing.
    act(() => setLiveStreamVia('direct'));
    expect(container.innerHTML).toBe('');

    appPath('ipv6');
    expect(container.textContent).toBe('直连');
    appPath('ipv4');
    expect(container.textContent).toBe('直连');

    // Through the relay: never 直连.
    appPath('relay');
    expect(container.textContent).toBe('中继');
    expect(screen.queryByText('直连')).toBeNull();

    // The page's stream is not on the direct ingress: nothing, whatever the App's path.
    act(() => setLiveStreamVia('other'));
    expect(container.innerHTML).toBe('');
    appPath('ipv4');
    expect(container.innerHTML).toBe('');
  });

  it('names the relay in English too', async () => {
    await i18n.changeLanguage('en');
    app.path = 'relay';
    act(() => setLiveStreamVia('direct'));
    const { container } = render(<DirectTag />);
    expect(container.textContent).toBe('Relay');
    appPath('ipv4');
    expect(container.textContent).toBe('Direct');
    await i18n.changeLanguage('zh');
  });
});
