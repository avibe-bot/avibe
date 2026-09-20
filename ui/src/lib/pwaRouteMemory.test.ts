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
    expect(normalizeRestorablePwaPath('/settings/shortcuts')).toBe('/settings/shortcuts');
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

  it('cold-launches an installed PWA back onto General through the whole chain', () => {
    // One test-owned store carries the actual write -> read -> resolve path, so
    // the allowlist entry is exercised where the product uses it rather than
    // only where it is declared.
    const store = new Map<string, string>();
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value); },
    };

    writeLastPwaPath('/settings/general?tab=theme#appearance', storage);
    expect(store.get('avibe.pwa.last-route.v1')).toBe('/settings/general');

    const remembered = readLastPwaPath(storage);
    expect(remembered).toBe('/settings/general');
    expect(resolvePwaLaunchPath(true, { pathname: '/', search: '', hash: '' }, remembered))
      .toBe('/settings/general');

    // The allowlist is still a list, not a prefix rule: an unknown sub-path is
    // rejected at the write, leaving the last restorable route intact.
    expect(normalizeRestorablePwaPath('/settings/general/theme')).toBeNull();
    writeLastPwaPath('/settings/general/theme', storage);
    expect(readLastPwaPath(storage)).toBe('/settings/general');
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
