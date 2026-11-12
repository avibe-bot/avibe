// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../i18n/en.json';
import { DESKTOP_OPEN_UPDATES_EVENT } from '../lib/desktopShell';
import { DesktopAccountRow } from './DesktopAccountRow';

const present = vi.hoisted(() => vi.fn());
vi.mock('./useDesktopClient', () => ({
  usePresentDesktopClient: () => present,
  useDesktopClientProjection: () => ({
    account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' } },
    connection: { status: 'remote_connected', committed: { kind: 'remote', name: 'Studio', instance_id: 'studio' }, pending: null },
    local: { name: '小麦的 Mac', remote_access: 'off' },
    desktop: { version: '3.2.6rc9', update: { phase: 'available', latest: '3.2.7' } },
    remote_auxiliary: 'disabled_incomplete',
  }),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('AUTH-SETUP-178 native menu opens a local-facts popover; its CTA presents one trusted updater decision', async () => {
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><DesktopAccountRow /></I18nextProvider>);
  const event = new Event(DESKTOP_OPEN_UPDATES_EVENT, { cancelable: true });
  fireEvent(window, event);
  expect(event.defaultPrevented).toBe(true);
  expect(window.__AVIBE_DESKTOP_UPDATES_HANDLED__).toBe(true);
  expect(present).not.toHaveBeenCalled();
  expect(await screen.findByText('3.2.6rc9 → 3.2.7')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Review and install update' }));
  expect(present).toHaveBeenCalledExactlyOnceWith('updates');
});
