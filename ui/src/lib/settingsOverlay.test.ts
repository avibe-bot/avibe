import { describe, expect, it, vi } from 'vitest';
import type { Location } from 'react-router-dom';

import {
  closeSettingsOverlay,
  isSettingsEntryPath,
  settingsOverlayHistoryDelta,
  settingsOverlayNavigationState,
  settingsOverlayOriginFromState,
  settingsOverlayKeepsRetainedRoute,
  type SettingsOverlayOrigin,
} from './settingsOverlay';

const origin = (historyIndex: number | null = 2): SettingsOverlayOrigin => ({
  historyIndex,
  location: {
    pathname: '/chat/ses_1',
    search: '?message=m1',
    hash: '#tail',
    state: { source: 'search' },
    key: 'chat-origin',
  } satisfies Location,
});

const location = (pathname: string, state: unknown = null): Location => ({
  pathname,
  search: '',
  hash: '',
  state,
  key: pathname,
});

describe('Settings overlay history', () => {
  it('unwinds every entry added after the opening route', () => {
    expect(settingsOverlayHistoryDelta(origin(), { idx: 5 })).toBe(-3);
    expect(settingsOverlayHistoryDelta(origin(), { idx: 2 })).toBeNull();
    expect(settingsOverlayHistoryDelta(origin(null), { idx: 5 })).toBeNull();

    const navigate = vi.fn();
    closeSettingsOverlay(navigate, origin(), { idx: 5 });
    expect(navigate).toHaveBeenCalledWith(-3);
  });

  it('falls back to replacing the exact origin when no history index exists', () => {
    const navigate = vi.fn();
    closeSettingsOverlay(navigate, origin(null), null);

    expect(navigate).toHaveBeenCalledWith('/chat/ses_1?message=m1#tail', {
      replace: true,
      state: { source: 'search' },
    });
  });
});

describe('Settings overlay navigation ownership', () => {
  it('attaches one origin at desktop ingress and carries it through Settings', () => {
    const firstState = settingsOverlayNavigationState({
      destinationPathname: '/settings/replies',
      desktop: true,
      historyState: { idx: 2 },
      source: origin().location,
      targetState: { draft: 'first' },
    });
    expect(settingsOverlayOriginFromState(firstState)).toEqual(origin());
    expect(firstState).toMatchObject({ draft: 'first' });

    const nextState = settingsOverlayNavigationState({
      destinationPathname: '/settings/diagnostics',
      desktop: true,
      source: location('/settings/replies', firstState),
      targetState: { draft: 'next' },
    });
    expect(settingsOverlayOriginFromState(nextState)).toEqual(origin());
    expect(nextState).toMatchObject({ draft: 'next' });
  });

  it('does not turn direct or non-Settings legacy redirects into origins', () => {
    expect(isSettingsEntryPath('/doctor')).toBe(true);
    expect(isSettingsEntryPath('/admin/show-pages')).toBe(false);

    const directRedirectState = settingsOverlayNavigationState({
      destinationPathname: '/settings/diagnostics',
      desktop: true,
      source: location('/doctor'),
      targetState: undefined,
    });
    expect(settingsOverlayOriginFromState(directRedirectState)).toBeNull();
  });

  it('leaves mobile Settings ingress from an ordinary route as a primary route', () => {
    const state = settingsOverlayNavigationState({
      destinationPathname: '/settings/replies',
      desktop: false,
      source: origin().location,
      targetState: undefined,
    });
    expect(settingsOverlayOriginFromState(state)).toBeNull();
  });

  it('retains the Workbench home behind mobile Settings and carries it onward', () => {
    const home = location('/');
    const state = settingsOverlayNavigationState({
      destinationPathname: '/settings/remote-access',
      desktop: false,
      historyState: { idx: 4 },
      source: home,
      targetState: undefined,
    });

    const retained = settingsOverlayOriginFromState(state);
    expect(retained).toEqual({ historyIndex: 4, location: home });

    // Settings-internal navigation keeps the same origin rather than minting a
    // second one, so Back still unwinds to the composer the user left.
    const onward = settingsOverlayNavigationState({
      destinationPathname: '/settings/general',
      desktop: false,
      historyState: { idx: 5 },
      source: location('/settings/remote-access', state),
      targetState: undefined,
    });
    expect(settingsOverlayOriginFromState(onward)).toEqual({ historyIndex: 4, location: home });

    // Closing from anywhere in that chain unwinds every entry back to home.
    const navigate = vi.fn();
    closeSettingsOverlay(navigate, settingsOverlayOriginFromState(onward)!, { idx: 6 });
    expect(navigate).toHaveBeenCalledWith(-2);
  });

  it('retains the setup wizard behind mobile Model Hub and returns to it', () => {
    const setup = location('/setup');
    const state = settingsOverlayNavigationState({
      destinationPathname: '/settings/models',
      desktop: false,
      historyState: { idx: 4 },
      source: setup,
      targetState: undefined,
    });

    expect(settingsOverlayOriginFromState(state)).toEqual({ historyIndex: 4, location: setup });

    const navigate = vi.fn();
    closeSettingsOverlay(navigate, settingsOverlayOriginFromState(state)!, { idx: 5 });
    expect(navigate).toHaveBeenCalledWith(-1);
  });

  it('keeps the origin across a retired-alias redirect', () => {
    const opened = settingsOverlayNavigationState({
      destinationPathname: '/settings/appearance',
      desktop: true,
      historyState: { idx: 2 },
      source: origin().location,
      targetState: undefined,
    });
    // `<Navigate to="/settings/general" replace />` runs through the same
    // boundary, so the page the alias lands on still knows what it covered.
    const redirected = settingsOverlayNavigationState({
      destinationPathname: '/settings/general',
      desktop: true,
      historyState: { idx: 3 },
      source: location('/settings/appearance', opened),
      targetState: undefined,
    });
    expect(settingsOverlayOriginFromState(redirected)).toEqual(origin());
  });

  it('invents no origin for a mobile Settings deep link', () => {
    const state = settingsOverlayNavigationState({
      destinationPathname: '/settings/general',
      desktop: false,
      source: location('/settings'),
      targetState: undefined,
    });
    expect(settingsOverlayOriginFromState(state)).toBeNull();
  });

  it('exempts exactly the transitions that keep the route behind Settings mounted', () => {
    const current = origin().location;
    const overlayState = settingsOverlayNavigationState({
      destinationPathname: '/settings/replies',
      desktop: true,
      historyState: { idx: 2 },
      source: current,
      targetState: undefined,
    });
    const replies = location('/settings/replies', overlayState);
    const nextSection = location('/settings/diagnostics', settingsOverlayNavigationState({
      destinationPathname: '/settings/diagnostics',
      desktop: true,
      source: replies,
      targetState: undefined,
    }));

    // Opening over the current entry, and only that entry.
    expect(settingsOverlayKeepsRetainedRoute(current, replies)).toBe(true);
    expect(settingsOverlayKeepsRetainedRoute(location('/chat/another'), replies)).toBe(false);
    expect(settingsOverlayKeepsRetainedRoute(current, location('/settings/replies'))).toBe(false);
    // Moving between sections over the same origin.
    expect(settingsOverlayKeepsRetainedRoute(replies, nextSection)).toBe(true);
    // Closing onto the origin, by history pop (same key) or by replace (new key).
    expect(settingsOverlayKeepsRetainedRoute(replies, current)).toBe(true);
    expect(settingsOverlayKeepsRetainedRoute(replies, { ...current, key: 'replaced' })).toBe(true);
    // Any other navigation made while Settings is open leaves the retained route.
    expect(settingsOverlayKeepsRetainedRoute(replies, location('/chat/another'))).toBe(false);
    expect(settingsOverlayKeepsRetainedRoute(replies, { ...current, hash: '' })).toBe(false);
    expect(settingsOverlayKeepsRetainedRoute(replies, location('/settings/general'))).toBe(false);
  });
});
