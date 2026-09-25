/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import type { Location, NavigateFunction } from 'react-router-dom';

import { DESKTOP_OPEN_SETTINGS_EVENT } from '@/lib/desktopShell';
import { closeSettingsOverlay, settingsOverlayOriginFromState } from '@/lib/settingsOverlay';
import { DesktopSettingsCommand } from './DesktopSettingsCommand';
import { SettingsOverlayNavigationBoundary } from './settings/SettingsOverlayNavigationBoundary';

const DESKTOP_SHELL_FLAG = '__AVIBE_DESKTOP_SHELL__';

const probe: { current: { location: Location; navigate: NavigateFunction } | null } = { current: null };

const Probe = () => {
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    probe.current = { location, navigate };
  });
  return null;
};

const renderShell = (entry: string) => render(
  <MemoryRouter initialEntries={[entry]}>
    <SettingsOverlayNavigationBoundary desktop>
      <DesktopSettingsCommand />
      <Probe />
    </SettingsOverlayNavigationBoundary>
  </MemoryRouter>,
);

// What the shell's Settings… item runs: the request first, a page load only
// when nothing in the page took it.
const requestSettings = (): boolean => {
  let taken = false;
  act(() => {
    taken = !window.dispatchEvent(new Event(DESKTOP_OPEN_SETTINGS_EVENT, { cancelable: true }));
  });
  return taken;
};

afterEach(() => {
  cleanup();
  probe.current = null;
  Reflect.deleteProperty(window, DESKTOP_SHELL_FLAG);
});

describe('DesktopSettingsCommand', () => {
  it('opens General Settings over the current surface and returns to it', () => {
    Object.defineProperty(window, DESKTOP_SHELL_FLAG, { value: true, configurable: true });
    renderShell('/chat/ses_7?message=m1');

    expect(requestSettings()).toBe(true);
    expect(probe.current?.location.pathname).toBe('/settings/general');

    const origin = settingsOverlayOriginFromState(probe.current?.location.state);
    expect(origin?.location.pathname).toBe('/chat/ses_7');
    expect(origin?.location.search).toBe('?message=m1');
    act(() => closeSettingsOverlay(probe.current!.navigate, origin!));
    expect(`${probe.current?.location.pathname}${probe.current?.location.search}`).toBe('/chat/ses_7?message=m1');
  });

  it('keeps the origin when Settings is already open on another section', () => {
    Object.defineProperty(window, DESKTOP_SHELL_FLAG, { value: true, configurable: true });
    renderShell('/chat/ses_7');
    act(() => probe.current!.navigate('/settings/service'));

    expect(requestSettings()).toBe(true);
    expect(probe.current?.location.pathname).toBe('/settings/general');
    expect(settingsOverlayOriginFromState(probe.current?.location.state)?.location.pathname).toBe('/chat/ses_7');
  });

  it('leaves the request untaken outside the desktop shell', () => {
    renderShell('/chat/ses_7');

    expect(requestSettings()).toBe(false);
    expect(probe.current?.location.pathname).toBe('/chat/ses_7');
  });
});
