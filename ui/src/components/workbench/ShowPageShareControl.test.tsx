/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { ShowPageShareControl } from './ShowPageShareControl';

const api = {
  ensureShowPage: vi.fn(),
  getShowPageAccess: vi.fn(),
  getShowPageAuthorizedEmails: vi.fn(),
  replaceShowPageAuthorizedEmails: vi.fn(),
  listOrganizationResources: vi.fn(),
  listOrganizationGroups: vi.fn(),
  setShowPageVisibility: vi.fn(),
  rotateShowPageShare: vi.fn(),
};

vi.mock('../../context/ApiContext', () => ({
  ApiError: class ApiError extends Error {
    code = null;
  },
  useApi: () => api,
}));

vi.mock('@/context/ApiContext', () => ({
  ApiError: class ApiError extends Error {
    code = null;
  },
  useApi: () => api,
}));

vi.mock('../../context/DockContext', () => ({
  useDock: () => ({
    isDocked: vi.fn(() => false),
    isPinned: vi.fn(() => false),
    dock: vi.fn(),
    pin: vi.fn(),
    undock: vi.fn(),
  }),
}));

vi.mock('../useShowPages', () => ({
  useShowPageInventory: () => ({
    pages: [],
    mergePage: vi.fn(),
    removePage: vi.fn(),
    reload: vi.fn(),
  }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderControl = (compact: boolean) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ShowPageShareControl sessionId="ses-1" compact={compact} />
    </I18nextProvider>,
  );

describe('ShowPageShareControl trigger presentation', () => {
  it('renders the header-sized trigger by default', () => {
    const html = renderControl(false);

    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain('aria-label="Share"');
    expect(html).toContain('size-7');
    expect(html).not.toContain('size-6');
  });

  it('renders the window-chrome trigger when compact', () => {
    const html = renderControl(true);

    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain('aria-label="Share"');
    // Mirrors the compact annotate control's title-bar styling (§ design q4E5l chrome).
    expect(html).toContain('size-6');
    expect(html).toContain('rounded-md');
    expect(html).toContain('text-muted');
    expect(html).toContain('hover:text-foreground');
    expect(html).not.toContain('size-7');
  });
});

describe('ShowPageShareControl payload sequencing without prior access', () => {
  it('loads the payload after access resolves for a granted non-manager', async () => {
    // The app-window call site: no initialAccess, no instance authority. The
    // payload read is gated on access, so it must start only once access
    // resolves — the first open still shows the link.
    api.getShowPageAccess.mockResolvedValue({
      ok: true,
      mode: 'local',
      can_use: true,
      can_manage: false,
      can_publish_public: false,
      public_link_enabled: false,
    });
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'private',
      active_url: '/show/ses-1/',
      share_id: null,
      url_available: true,
      offline: false,
      title: null,
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => {
      expect(api.ensureShowPage).toHaveBeenCalledWith('ses-1');
    });
    await waitFor(() => {
      expect((screen.getByDisplayValue(/\/show\/ses-1\//) as HTMLInputElement).value).toContain('/show/ses-1/');
    });
  });

  it('never loads the payload when access resolves without page use', async () => {
    api.getShowPageAccess.mockResolvedValue({
      ok: true,
      mode: 'local',
      can_use: false,
      can_manage: true,
      can_publish_public: false,
      public_link_enabled: false,
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => {
      expect(api.getShowPageAccess).toHaveBeenCalledWith('ses-1');
    });
    // Metadata-only manager: no payload read is ever issued.
    await vi.waitFor(
      () => {
        expect(api.ensureShowPage).not.toHaveBeenCalled();
      },
      { timeout: 250 },
    );
  });

  it('issues exactly one payload read on the authorized parallel path', async () => {
    // A caller holding access (the chat header passes initialAccess) reads the
    // payload and access in parallel — the post-access hook must NOT fire a
    // second ensure request.
    api.getShowPageAccess.mockResolvedValue({
      ok: true,
      mode: 'local',
      can_use: true,
      can_manage: true,
      can_publish_public: true,
      public_link_enabled: false,
    });
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'private',
      active_url: '/show/ses-1/',
      share_id: null,
      url_available: true,
      offline: false,
      title: null,
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl
          sessionId="ses-1"
          initialAccess={{
            ok: true,
            mode: 'local',
            can_use: true,
            can_manage: true,
            can_publish_public: true,
            public_link_enabled: false,
          } as never}
        />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => {
      expect(api.getShowPageAccess).toHaveBeenCalled();
    });
    await vi.waitFor(() => {
      expect(api.ensureShowPage).toHaveBeenCalledTimes(1);
    });
  });

  it('shows the loading state while a first access read is pending', async () => {
    // The app-window caller has no access yet: while the access read is in
    // flight the popover must show the loading row, not the load-error text.
    let release: (() => void) | null = null;
    api.getShowPageAccess.mockImplementation(
      () => new Promise((resolve) => {
        release = () => resolve({
          ok: true,
          mode: 'local',
          can_use: false,
          can_manage: true,
          can_publish_public: false,
          public_link_enabled: false,
        });
      }),
    );

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => {
      expect(screen.getByText('Loading...')).toBeTruthy();
      expect(screen.queryByText("Couldn't load this Show Page.")).toBeNull();
    });

    (release ?? (() => undefined))();
    await waitFor(() => {
      expect(api.getShowPageAccess).toHaveBeenCalled();
    });
  });
});
