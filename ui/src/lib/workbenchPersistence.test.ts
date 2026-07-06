import { describe, expect, it } from 'vitest';

import {
  MAX_RESTORED_WINDOWS,
  WINDOW_RESTORE_PARAM,
  parseWorkbenchWindows,
  serializeWorkbenchWindows,
  stripRestoreParam,
  type PersistedWindow,
} from './workbenchPersistence';

const bounds = { x: 120, y: 80, width: 760, height: 520 };

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

  it('caps the number of restored windows against a corrupt/oversized array', () => {
    const windows = Array.from({ length: MAX_RESTORED_WINDOWS + 25 }, (_, i) => persisted({ id: `win-${i}`, appId: 'files' }));
    expect(parseWorkbenchWindows(serializeWorkbenchWindows(windows))).toHaveLength(MAX_RESTORED_WINDOWS);
  });

  it('rejects ids that are not the generated win-<number> shape', () => {
    // A corrupt id with selector syntax would break `[data-window-id="…"]` queries in the Dock.
    for (const id of ['win-1"]', 'evil', 'win-', 'win-1a', '__proto__']) {
      expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: [persisted({ id })] }))).toEqual([]);
    }
  });

  it('rejects id suffixes that are unsafe or lack increment headroom', () => {
    // Beyond MAX_SAFE_INTEGER, and exactly at it (no headroom for ++idSeq) — both dropped.
    expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: [persisted({ id: 'win-9007199254740992' })] }))).toEqual([]);
    expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: [persisted({ id: 'win-9007199254740991' })] }))).toEqual([]);
  });

  it('rejects unsafe / non-integer z values (they seed the focus counter)', () => {
    for (const z of [Number.MAX_SAFE_INTEGER, 1.5, -1, Number.NaN]) {
      expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: [persisted({ z })] }))).toEqual([]);
    }
    expect(parseWorkbenchWindows(serializeWorkbenchWindows([persisted({ z: 7 })]))).toHaveLength(1);
  });

  it('drops duplicate ids so restored windows never collide on key / guard maps', () => {
    const raw = JSON.stringify({
      version: 1,
      windows: [persisted({ id: 'win-1', appId: 'files' }), persisted({ id: 'win-1', appId: 'editor' }), persisted({ id: 'win-2', appId: 'terminal' })],
    });
    // First win-1 wins; the duplicate is dropped; win-2 is kept.
    expect(parseWorkbenchWindows(raw).map((w) => `${w.id}:${w.appId}`)).toEqual(['win-1:files', 'win-2:terminal']);
  });

  it('rejects nonsensical bounds (zero / negative / enormous) while keeping sane ones', () => {
    const bad = [
      { x: 0, y: 0, width: 0, height: 400 },
      { x: 0, y: 0, width: -800, height: 400 },
      { x: 0, y: 0, width: 500, height: 10 }, // below MIN_H
      { x: 0, y: 0, width: 1e9, height: 400 },
    ];
    for (const b of bad) {
      expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: [persisted({ bounds: b })] }))).toEqual([]);
    }
    expect(parseWorkbenchWindows(serializeWorkbenchWindows([persisted()]))).toHaveLength(1);
  });

  it('rejects inherited Object keys as appIds (own registry keys only)', () => {
    // A corrupt/hostile blob must not sneak "toString" / "__proto__" through an `in` check —
    // APP_REGISTRY[thatKey] would resolve to a non-app value and crash the window layer.
    for (const appId of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(parseWorkbenchWindows(JSON.stringify({ version: 1, windows: [persisted({ appId })] }))).toEqual([]);
    }
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
