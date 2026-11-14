// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import { usePresentDesktopClient } from './useDesktopClient';

const guard = vi.hoisted(() => ({ navigate: null as (() => void) | null }));
const presented = vi.hoisted(() => vi.fn());
vi.mock('../context/useUnsavedChangesActionGuard', () => ({
  useUnsavedChangesActionGuard: () => () => ({
    runNavigation: (callback: () => void) => { guard.navigate = callback; },
  }),
}));
vi.mock('./bridge', async (original) => ({
  ...(await original<typeof import('./bridge')>()),
  presentDesktopClient: presented,
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); presented.mockClear(); });

// A delayed unsaved-work decision must carry the originating Workbench locale,
// rather than reading a later language when navigation finally runs.
it('captures locale with the guarded intent', () => {
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  i18n.resolvedLanguage = 'zh-CN';
  function Probe() {
    const present = usePresentDesktopClient();
    return <button onClick={() => present('instances', 'native-1')}>Open</button>;
  }
  render(<Probe />);
  screen.getByText('Open').click();
  i18n.resolvedLanguage = 'en';
  expect(presented).not.toHaveBeenCalled();
  guard.navigate?.();
  expect(presented).toHaveBeenCalledExactlyOnceWith('instances', 'native-1', 'zh');
});
