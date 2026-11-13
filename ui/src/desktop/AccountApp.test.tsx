// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../i18n/en.json';
import { AccountApp } from './AccountApp';

const send = vi.hoisted(() => vi.fn(async () => null));
vi.mock('./useAccountClient', () => ({
  useAccountClient: () => ({
    error: null,
    send,
    state: {
      version: 1, revision: 1, generation: 4,
      account: { status: 'signed_in', identity: { id: 'fixture', email: 'max@example.test' }, email: null, gate: null, methods: [] },
      connection: { status: 'remote_connected', committed: { kind: 'remote', name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio.example' }, pending: null },
      enrollment: { status: 'unknown', instance_id: null, error_code: null },
      catalog: { status: 'idle', instances: [] },
      local: { name: '小麦的 Mac', remote_access: 'off' },
      desktop: {
        version: '3.2.6',
        update: { phase: 'available', latest: '3.2.7', channel: 'stable', prereleases: false, autoUpdate: false, hostActions: ['check', 'prereleases', 'auto_update'], skippedVersion: null, interactive: true },
      },
      consent: null,
      remote_auxiliary: 'disabled_incomplete',
    },
  }),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); window.history.replaceState(null, '', '/'); });

// ui/design: update/panel/trusted-view
it('AUTH-SETUP-178 the trusted update view acts on this computer\'s desktop app with the same panel', async () => {
  window.history.replaceState(null, '', '/account/index.html?view=updates');
  const i18n = createInstance(); await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
  render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  expect(screen.getByText(en.update.trustedView.title)).toBeTruthy();
  expect(screen.getByText(en.update.status.available)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: en.update.action.update }));
  expect(send).toHaveBeenCalledWith({ type: 'update_install', target_version: '3.2.7' });
});
