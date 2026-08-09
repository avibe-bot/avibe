import { describe, expect, it } from 'vitest';

import {
  appLaunchIntent,
  appTabHref,
  appTabHrefForDockId,
  isAppleContextClick,
  isStandaloneAppTab,
  tabModifierLabel,
} from './appLaunch';
import { showDockId } from '../context/dockDoc';

const mods = (overrides: Partial<Record<'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey', boolean>> = {}) => ({
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...overrides,
});

const APPLE = true;
const NON_APPLE = false;

describe('appLaunchIntent', () => {
  it('opens a browser tab on the platform tab modifier: ⌘ on Apple, Ctrl elsewhere', () => {
    expect(appLaunchIntent(mods({ metaKey: true }), APPLE)).toBe('newTab');
    expect(appLaunchIntent(mods({ ctrlKey: true }), NON_APPLE)).toBe('newTab');
  });

  it('does not treat the other platform’s modifier as the tab modifier', () => {
    // macOS Ctrl+click is the right-click gesture, not a new tab.
    expect(appLaunchIntent(mods({ ctrlKey: true }), APPLE)).not.toBe('newTab');
    expect(appLaunchIntent(mods({ metaKey: true }), NON_APPLE)).not.toBe('newTab');
    // Both held is ambiguous — neither platform claims it as the tab modifier.
    expect(appLaunchIntent(mods({ metaKey: true, ctrlKey: true }), APPLE)).not.toBe('newTab');
    expect(appLaunchIntent(mods({ metaKey: true, ctrlKey: true }), NON_APPLE)).not.toBe('newTab');
  });

  it('keeps Alt (and Shift) on a new workbench window', () => {
    expect(appLaunchIntent(mods({ altKey: true }), APPLE)).toBe('newWindow');
    expect(appLaunchIntent(mods({ shiftKey: true }), NON_APPLE)).toBe('newWindow');
  });

  it('prefers the tab when the tab modifier is combined with Alt/Shift', () => {
    expect(appLaunchIntent(mods({ metaKey: true, altKey: true }), APPLE)).toBe('newTab');
    expect(appLaunchIntent(mods({ ctrlKey: true, shiftKey: true }), NON_APPLE)).toBe('newTab');
  });

  it('activates on a plain click, and on a Ctrl+Enter keypress on Apple', () => {
    expect(appLaunchIntent(mods(), APPLE)).toBe('activate');
    expect(appLaunchIntent(mods({ ctrlKey: true }), APPLE)).toBe('activate');
  });
});

describe('isAppleContextClick', () => {
  it('flags a Ctrl-held mouse click on Apple only', () => {
    expect(isAppleContextClick(mods({ ctrlKey: true }), APPLE)).toBe(true);
    expect(isAppleContextClick(mods({ ctrlKey: true }), NON_APPLE)).toBe(false);
    expect(isAppleContextClick(mods({ metaKey: true }), APPLE)).toBe(false);
    expect(isAppleContextClick(mods(), APPLE)).toBe(false);
  });

  it('ignores a KEYBOARD-synthesized click (detail 0) — Ctrl+Enter must still launch', () => {
    // Enter/Space on a <button> fires a click with detail 0; a real mouse click is ≥1.
    expect(isAppleContextClick({ ...mods({ ctrlKey: true }), detail: 0 }, APPLE)).toBe(false);
    expect(isAppleContextClick({ ...mods({ ctrlKey: true }), detail: 1 }, APPLE)).toBe(true);
  });
});

describe('tabModifierLabel', () => {
  it('names the key the hint should show, per platform', () => {
    expect(tabModifierLabel(APPLE)).toBe('⌘');
    expect(tabModifierLabel(NON_APPLE)).toBe('Ctrl');
  });

  it('agrees with the intent rule: the labelled key is the one that opens a tab', () => {
    for (const isApple of [APPLE, NON_APPLE]) {
      const held = tabModifierLabel(isApple) === '⌘' ? { metaKey: true } : { ctrlKey: true };
      expect(appLaunchIntent(mods(held), isApple)).toBe('newTab');
    }
  });
});

describe('appTabHref', () => {
  it('maps a built-in that stands alone to its /apps/<id> route, flagged as a single-app tab', () => {
    expect(appTabHref({ appId: 'files' })).toBe('/apps/files?standalone=1');
    expect(appTabHref({ appId: 'terminal' })).toBe('/apps/terminal?standalone=1');
    expect(appTabHref({ appId: 'editor' })).toBe('/apps/editor?standalone=1');
  });

  it('emits a flag the shell actually recognizes', () => {
    // The href and the shell-side reader must not drift: a silently-unread param
    // would restore the saved windows over the very app the tab was opened for.
    for (const appId of ['files', 'terminal', 'editor']) {
      const href = appTabHref({ appId })!;
      expect(isStandaloneAppTab(href.slice(href.indexOf('?')))).toBe(true);
    }
  });

  it('has no tab surface for a built-in whose route is not standalone on desktop', () => {
    // /apps/library opens a Library WINDOW and redirects the tab to `/`; preview has
    // no route at all. Both must fall back to a workbench window.
    expect(appTabHref({ appId: 'library' })).toBeNull();
    expect(appTabHref({ appId: 'preview' })).toBeNull();
    expect(appTabHref({ appId: 'not-an-app' })).toBeNull();
  });

  it('maps a Show Page to its private /show/<sid>/ page', () => {
    expect(appTabHref({ appId: 'showpage', sessionId: 'abc123' })).toBe('/show/abc123/');
  });

  it('encodes an unsafe session id', () => {
    expect(appTabHref({ appId: 'showpage', sessionId: 'a b/c' })).toBe('/show/a%20b%2Fc/');
  });

  it('has no tab surface for a Show Page without a session id', () => {
    expect(appTabHref({ appId: 'showpage' })).toBeNull();
    expect(appTabHref({ appId: 'showpage', sessionId: '  ' })).toBeNull();
    expect(appTabHref({ appId: '' })).toBeNull();
  });

  it('keeps every internal app in the installed iOS PWA', () => {
    const iosPwa = { iosStandalone: true };

    expect(appTabHref({ appId: 'showpage', sessionId: 'abc123' }, iosPwa)).toBeNull();
    expect(appTabHref({ appId: 'files' }, iosPwa)).toBeNull();
    expect(appTabHref({ appId: 'terminal' }, iosPwa)).toBeNull();
    expect(appTabHref({ appId: 'editor' }, iosPwa)).toBeNull();
  });
});

describe('isStandaloneAppTab', () => {
  it('recognizes only the tab flag', () => {
    expect(isStandaloneAppTab('?standalone=1')).toBe(true);
    expect(isStandaloneAppTab('?view=pages&standalone=1')).toBe(true);
    expect(isStandaloneAppTab('?standalone=0')).toBe(false);
    expect(isStandaloneAppTab('?standalone')).toBe(false);
    expect(isStandaloneAppTab('?view=pages')).toBe(false);
    expect(isStandaloneAppTab('')).toBe(false);
  });

  it('is false for an in-shell navigation to the same route', () => {
    // The sidebar Apps launcher and a chat's "open in editor" land on /apps/editor
    // WITHOUT the flag; those must keep the workbench windows they navigated from.
    expect(isStandaloneAppTab(new URL('http://x/apps/editor').search)).toBe(false);
  });
});

describe('appTabHrefForDockId', () => {
  it('resolves both Dock id kinds', () => {
    expect(appTabHrefForDockId('terminal')).toBe('/apps/terminal?standalone=1');
    expect(appTabHrefForDockId(showDockId('sess42'))).toBe('/show/sess42/');
    expect(appTabHrefForDockId('library')).toBeNull();
  });

  it('withdraws new-tab targets for every Dock app in the installed iOS PWA', () => {
    const iosPwa = { iosStandalone: true };

    expect(appTabHrefForDockId('terminal', iosPwa)).toBeNull();
    expect(appTabHrefForDockId(showDockId('sess42'), iosPwa)).toBeNull();
  });
});
