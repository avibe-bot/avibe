/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { NativeAppLaunchFallback } from './NativeAppLaunchFallback';
import { WindowManagerProvider } from '../../context/WindowManagerProvider';
import { useWindowManager } from '../../context/WindowManagerContext';
import { StandaloneAppTabContext } from '../../context/StandaloneAppTabContext';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import type { AppLaunchSnapshot } from '../../apps/appLaunchProtocol';
const snapshot = (): AppLaunchSnapshot => ({ appId: 'showpage', requestId: crypto.randomUUID(), windowId: 'runtime-published', sessionId: 'ses-fixture', source: 'deep-link' });
const Probe = () => {
  const manager = useWindowManager();
  const [draft, setDraft] = useState('');
  return <><input aria-label="Draft" value={draft} onChange={(event) => setDraft(event.target.value)} /><output>{manager.windows.map(win => `${win.appId}:${win.params?.sessionId}`).join(',')}</output></>;
};
afterEach(() => { cleanup(); delete window.__AVIBE_APP_LAUNCH__; delete window.__AVIBE_APP_LAUNCH_PROTOCOL__; });
it('consumes a late-mounted terminal fallback once across remounts and preserves the existing draft', () => {
  window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1;
  const next = snapshot();
  window.__AVIBE_APP_LAUNCH__ = next;
  const announce = vi.fn();
  const tree = (show: boolean) => <MemoryRouter><WindowManagerProvider standalone onWindowForeground={announce}><RouteSurfaceActiveContext.Provider value={false}>{show && <NativeAppLaunchFallback />}<Probe /></RouteSurfaceActiveContext.Provider></WindowManagerProvider></MemoryRouter>;
  const { rerender } = render(tree(false));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'nonempty retained draft' } });
  const submit = vi.spyOn(HTMLAnchorElement.prototype, 'click');
  rerender(tree(true));
  expect(screen.getByText('showpage:ses-fixture')).toBeTruthy();
  rerender(tree(false)); rerender(tree(true));
  act(() => window.dispatchEvent(new CustomEvent('avibe:app-launch', { detail: next })));
  expect(screen.getByText('showpage:ses-fixture')).toBeTruthy();
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('nonempty retained draft');
  expect(announce).not.toHaveBeenCalled();
  expect(submit).not.toHaveBeenCalled();
  submit.mockRestore();
});
it('does not consume the main fallback in a standalone child', () => {
  window.__AVIBE_APP_LAUNCH_PROTOCOL__ = 1;
  window.__AVIBE_APP_LAUNCH__ = snapshot();
  render(<MemoryRouter><WindowManagerProvider standalone><StandaloneAppTabContext.Provider value><NativeAppLaunchFallback /><Probe /></StandaloneAppTabContext.Provider></WindowManagerProvider></MemoryRouter>);
  expect(screen.queryByText('showpage:ses-fixture')).toBeNull();
});
