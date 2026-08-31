import { describe, expect, it, vi } from 'vitest';
import type { Location } from 'react-router-dom';

import {
  closeSettingsOverlay,
  isSettingsEntryPath,
  settingsOverlayHistoryDelta,
  settingsOverlayNavigationState,
  settingsOverlayOriginFromState,
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

  it('leaves mobile Settings ingress as a primary route', () => {
    const state = settingsOverlayNavigationState({
      destinationPathname: '/settings/replies',
      desktop: false,
      source: origin().location,
      targetState: undefined,
    });
    expect(settingsOverlayOriginFromState(state)).toBeNull();
  });
});
