import { describe, expect, it, vi } from 'vitest';

import {
  normalizeRestorablePwaPath,
  readLastPwaPath,
  resolvePwaLaunchPath,
  shouldRestorePwaLaunch,
  writeLastPwaPath,
} from './pwaRouteMemory';

describe('PWA route memory', () => {
  it('restores canonical and dynamic app pages while dropping URL state', () => {
    expect(normalizeRestorablePwaPath('/inbox')).toBe('/inbox');
    expect(normalizeRestorablePwaPath('/chat/session-123?from=push#latest')).toBe('/chat/session-123');
    expect(normalizeRestorablePwaPath('/admin/settings/backends/codex')).toBe(
      '/admin/settings/backends/codex',
    );
  });

  it('rejects setup, retired, unknown, and cross-origin paths', () => {
    expect(normalizeRestorablePwaPath('/setup')).toBeNull();
    expect(normalizeRestorablePwaPath('/more')).toBeNull();
    expect(normalizeRestorablePwaPath('/unknown')).toBeNull();
    expect(normalizeRestorablePwaPath('//example.com/inbox')).toBeNull();
    expect(normalizeRestorablePwaPath('https://example.com/inbox')).toBeNull();
  });

  it('restores only an installed PWA launched at the manifest root', () => {
    const root = { pathname: '/', search: '', hash: '' };
    expect(resolvePwaLaunchPath(true, root, '/chat/session-123')).toBe('/chat/session-123');
    expect(resolvePwaLaunchPath(false, root, '/chat/session-123')).toBeNull();
    expect(resolvePwaLaunchPath(true, { ...root, pathname: '/inbox' }, '/chat/session-123')).toBeNull();
    expect(resolvePwaLaunchPath(true, { ...root, search: '?login=1' }, '/chat/session-123')).toBeNull();
    expect(resolvePwaLaunchPath(true, root, '/')).toBeNull();
  });

  it('does not hijack a later in-app navigation back to the workbench root', () => {
    expect(shouldRestorePwaLaunch('/inbox', 'default', { key: 'default', pathname: '/' })).toBe(true);
    expect(shouldRestorePwaLaunch('/inbox', 'default', { key: 'later', pathname: '/' })).toBe(false);
  });

  it('reads and writes through best-effort storage', () => {
    const getItem = vi.fn(() => '/apps/files');
    const setItem = vi.fn();

    expect(readLastPwaPath({ getItem })).toBe('/apps/files');
    writeLastPwaPath('/chat/session-456?ignored=1', { setItem });

    expect(getItem).toHaveBeenCalledOnce();
    expect(setItem).toHaveBeenCalledWith('avibe.pwa.last-route.v1', '/chat/session-456');
  });

  it('tolerates unavailable browser storage', () => {
    expect(
      readLastPwaPath({
        getItem: () => {
          throw new Error('blocked');
        },
      }),
    ).toBeNull();
    expect(() =>
      writeLastPwaPath('/inbox', {
        setItem: () => {
          throw new Error('blocked');
        },
      }),
    ).not.toThrow();
  });
});
