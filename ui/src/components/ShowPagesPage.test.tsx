/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../i18n/en.json';
import type { ShowPage } from '../lib/showPagesStore';
import { ShowPagesView } from './ShowPagesPage';

vi.mock('../context/DockContext', () => ({
  useDock: () => ({
    isPinned: () => false,
    pin: vi.fn(),
    unpin: vi.fn(),
  }),
}));

vi.mock('../context/WindowManagerContext', () => ({
  useWindowManager: () => ({
    windows: [],
    setParams: vi.fn(),
    setTitle: vi.fn(),
  }),
}));

vi.mock('./workbench/ShowPageSharingSettings', () => ({
  ShowPageSharingSettings: () => null,
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const page = (offline: boolean): ShowPage => ({
  session_id: 'ses-1',
  visibility: offline ? 'offline' : 'private',
  access_mode: 'private',
  access_revision: 0,
  can_manage: true,
  can_publish_public: true,
  title: 'Demo page',
  platform: 'web',
  agent: 'codex',
  path: '/tmp/show/ses-1',
  icon_version: null,
  active_url: offline ? null : '/show/ses-1/',
  private_url: '/show/ses-1/',
  public_url: null,
  url_available: true,
  share_id: 'stable-link',
  offline,
  offline_at: offline ? '2026-08-18T00:00:00Z' : null,
  created_at: '2026-08-17T00:00:00Z',
  updated_at: '2026-08-18T00:00:00Z',
});

const renderView = (offline: boolean, setOffline: ReturnType<typeof vi.fn>) => render(
  <MemoryRouter>
    <I18nextProvider i18n={i18n}>
      <ShowPagesView
        pages={[page(offline)]}
        loading={false}
        loaded
        busyId={null}
        setOffline={setOffline}
        rename={vi.fn()}
        uploadIcon={vi.fn()}
        reload={vi.fn()}
      />
    </I18nextProvider>
  </MemoryRouter>,
);

afterEach(cleanup);

describe('ShowPagesView availability control', () => {
  it('treats an enabled switch as online and turning it off as going offline', () => {
    const setOffline = vi.fn();
    renderView(false, setOffline);
    fireEvent.click(screen.getByRole('button', { name: 'Details' }));

    const toggle = screen.getByRole('switch', { name: 'Page online' });
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText('This page is online. Turn it off to stop serving it.')).toBeTruthy();
    fireEvent.click(toggle);

    expect(setOffline).toHaveBeenCalledWith(page(false), true);
  });

  it('treats a disabled switch as offline and turning it on as restoring access', () => {
    const setOffline = vi.fn();
    renderView(true, setOffline);
    fireEvent.click(screen.getByRole('button', { name: 'Details' }));

    const toggle = screen.getByRole('switch', { name: 'Page online' });
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(screen.getByText('This page is offline. Turn it on to restore access.')).toBeTruthy();
    fireEvent.click(toggle);

    expect(setOffline).toHaveBeenCalledWith(page(true), false);
  });
});
