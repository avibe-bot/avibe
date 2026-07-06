import { describe, expect, it } from 'vitest';

import {
  WINDOW_RESTORE_PARAM,
  parseWorkbenchWindows,
  serializeWorkbenchWindows,
  stripRestoreParam,
  type PersistedWindow,
} from './workbenchPersistence';

const bounds = { x: 10, y: 20, width: 300, height: 200 };

function persisted(overrides: Partial<PersistedWindow> = {}): PersistedWindow {
  return {
    id: 'win-1',
    appId: 'editor',
    bounds,
    z: 1,
    minimized: false,
    maximized: false,
    ...overrides,
  };
}

describe('workbench window persistence — round trip', () => {
  it('serializes and parses a window back to the same rehydratable fields', () => {
    const windows: PersistedWindow[] = [
      persisted({ id: 'win-3', appId: 'terminal', title: 'build', z: 5, minimized: true }),
    ];
    const [w] = parseWorkbenchWindows(serializeWorkbenchWindows(windows));
    expect(w).toMatchObject({ id: 'win-3', appId: 'terminal', title: 'build', z: 5, minimized: true, maximized: false, bounds });
  });

  it('hands a window body its appState back through params under the restore key', () => {
    const appState = { root: '/src', tabs: [{ path: '/src/a.ts', name: 'a.ts' }], activePath: '/src/a.ts' };
    const [w] = parseWorkbenchWindows(serializeWorkbenchWindows([persisted({ appState })]));
    expect(w.params?.[WINDOW_RESTORE_PARAM]).toEqual(appState);
  });

  it('merges appState alongside original launch params without dropping them', () => {
    const raw = serializeWorkbenchWindows([
      persisted({ appId: 'preview', params: { path: '/x.png', name: 'x.png' }, appState: { seen: true } }),
    ]);
    const [w] = parseWorkbenchWindows(raw);
    expect(w.params).toEqual({ path: '/x.png', name: 'x.png', [WINDOW_RESTORE_PARAM]: { seen: true } });
  });

  it('preserves restoreBounds for a maximized window', () => {
    const restoreBounds = { x: 5, y: 6, width: 700, height: 500 };
    const [w] = parseWorkbenchWindows(serializeWorkbenchWindows([persisted({ maximized: true, restoreBounds })]));
    expect(w.restoreBounds).toEqual(restoreBounds);
  });
});

describe('workbench window persistence — corrupt / old data is ignored', () => {
  it('returns [] for invalid JSON', () => {
    expect(parseWorkbenchWindows('{not json')).toEqual([]);
  });

  it('returns [] for null / empty input', () => {
    expect(parseWorkbenchWindows(null)).toEqual([]);
    expect(parseWorkbenchWindows(undefined)).toEqual([]);
    expect(parseWorkbenchWindows('')).toEqual([]);
  });

  it('returns [] for a mismatched schema version', () => {
    expect(parseWorkbenchWindows(JSON.stringify({ version: 2, windows: [persisted()] }))).toEqual([]);
  });

  it('returns [] when windows is not an array', () => {
    expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: {} }))).toEqual([]);
  });

  it('drops entries with an unknown appId but keeps valid siblings', () => {
    const raw = JSON.stringify({
      version: 1,
      windows: [persisted({ id: 'win-1', appId: 'ghost-app' }), persisted({ id: 'win-2', appId: 'files' })],
    });
    const result = parseWorkbenchWindows(raw);
    expect(result.map((w) => w.id)).toEqual(['win-2']);
  });

  it('drops entries with malformed bounds or missing fields', () => {
    const raw = JSON.stringify({
      version: 1,
      windows: [
        { id: 'win-1', appId: 'editor', bounds: { x: 'nope', y: 0, width: 1, height: 1 }, z: 1, minimized: false, maximized: false },
        { id: 'win-2', appId: 'editor', z: 1, minimized: false, maximized: false }, // no bounds
        persisted({ id: 'win-3', appId: 'files' }),
      ],
    });
    expect(parseWorkbenchWindows(raw).map((w) => w.id)).toEqual(['win-3']);
  });

  it('does not copy unexpected keys off a stored blob onto the runtime window', () => {
    const raw = JSON.stringify({
      version: 1,
      windows: [{ ...persisted(), evil: 'x', z: 2 }],
    });
    const [w] = parseWorkbenchWindows(raw);
    expect(w).not.toHaveProperty('evil');
  });
});

describe('stripRestoreParam', () => {
  it('removes the injected restore key but keeps real launch params', () => {
    expect(stripRestoreParam({ path: '/a.ts', [WINDOW_RESTORE_PARAM]: { x: 1 } })).toEqual({ path: '/a.ts' });
  });

  it('returns undefined when the restore key was the only entry', () => {
    expect(stripRestoreParam({ [WINDOW_RESTORE_PARAM]: { x: 1 } })).toBeUndefined();
  });

  it('passes through params with no restore key unchanged', () => {
    const p = { path: '/a.ts' };
    expect(stripRestoreParam(p)).toBe(p);
    expect(stripRestoreParam(undefined)).toBeUndefined();
  });
});
