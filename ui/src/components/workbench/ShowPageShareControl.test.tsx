/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import type { ShowPageAccess } from '../../lib/showPageAccess';
import { ShowPageShareControl } from './ShowPageShareControl';

const permissionsApi = vi.hoisted(() => ({
  getPermissions: vi.fn(),
  getResourceAccess: vi.fn(),
  updateResourceAccess: vi.fn(),
}));

vi.mock('@/features/permissions/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/permissions/api')>()),
  getPermissions: permissionsApi.getPermissions,
  getResourceAccess: permissionsApi.getResourceAccess,
  updateResourceAccess: permissionsApi.updateResourceAccess,
}));

const api = {
  ensureShowPage: vi.fn(),
  getShowPageAccess: vi.fn(),
  getShowAccessSettings: vi.fn(),
  applyShowAccess: vi.fn(),
  listOrganizationResources: vi.fn(),
  listOrganizationGroups: vi.fn(),
  setShowPageAvailability: vi.fn(),
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

beforeEach(() => {
  permissionsApi.getPermissions.mockReset();
  permissionsApi.getResourceAccess.mockReset();
  permissionsApi.updateResourceAccess.mockReset();
});

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

const showPageAccess = (overrides: Partial<ShowPageAccess> = {}): ShowPageAccess => ({
  ok: true,
  mode: 'unmanaged',
  ownership_status: 'unmanaged',
  instance_id: null,
  organization_id: null,
  policy_organization_id: null,
  access_level: 'private',
  group_ids: [],
  policy_revision: null,
  last_applied_control_plane_revision: null,
  can_use: true,
  can_manage: false,
  can_publish_public: false,
  ...overrides,
});

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
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: false,
      can_publish_public: false,
    }));
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

  it('keeps Link access usable while Organization access is still loading', async () => {
    const organizationAccess = showPageAccess({
      mode: 'organization',
      ownership_status: 'unchanged',
      instance_id: 'inst-1',
      organization_id: 'org-1',
      policy_organization_id: 'org-1',
      can_manage: true,
      can_publish_public: true,
    });
    const pending = new Promise(() => undefined);
    permissionsApi.getPermissions.mockReturnValue(pending);
    permissionsApi.getResourceAccess.mockReturnValue(pending);
    api.getShowPageAccess.mockResolvedValue(organizationAccess);
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'private',
      active_url: '/show/ses-1/',
      share_id: null,
      url_available: true,
      offline: false,
      title: null,
    });
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'private',
        share_id: null,
        revision: 3,
        normalized_emails: [],
      },
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl
          sessionId="ses-1"
          initialAccess={organizationAccess}
          canManageInstance
        />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    expect(await screen.findByRole('button', { name: 'Access: Private' })).toBeTruthy();
    expect(screen.getByText('Loading Organization access…')).toBeTruthy();
    expect(api.getShowAccessSettings).toHaveBeenCalledWith('ses-1');
    expect(permissionsApi.getPermissions).toHaveBeenCalledOnce();
    expect(permissionsApi.getResourceAccess).toHaveBeenCalledWith({
      resource_kind: 'show_page',
      resource_id: 'ses-1',
    });
    const popover = document.querySelector('.overflow-y-auto');
    expect(popover?.classList.contains('max-h-[var(--radix-popover-content-available-height)]')).toBe(true);
  });

  it('never loads the payload when access resolves without page use', async () => {
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: false,
      can_manage: true,
      can_publish_public: false,
    }));

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
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: true,
      can_publish_public: true,
    }));
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
          initialAccess={showPageAccess({
            can_use: true,
            can_manage: true,
            can_publish_public: true,
          })}
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

  it('presents the Limited guest-admission link', async () => {
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: false,
      can_publish_public: false,
    }));
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'limited',
      active_url: null,
      public_url: 'https://alice.avibe.bot/p/stable-link/',
      share_id: 'stable-link',
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

    expect(await screen.findByDisplayValue('https://alice.avibe.bot/p/stable-link/')).toBeTruthy();
    const openLink = screen.getByRole('link', { name: 'Open' });
    expect(openLink.getAttribute('href')).toBe('https://alice.avibe.bot/p/stable-link/');
    expect(openLink.getAttribute('target')).toBe('_blank');
    expect(screen.getByRole('button', { name: 'Copy link' })).toBeTruthy();
  });

  it('keeps the Limited share action disabled without Cloud identity', async () => {
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: false,
      can_publish_public: false,
    }));
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'limited',
      active_url: null,
      public_url: null,
      share_id: 'stable-link',
      url_available: false,
      offline: false,
      title: null,
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => expect(api.ensureShowPage).toHaveBeenCalledWith('ses-1'));
    expect((screen.getByRole('button', { name: 'Open' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Copy link' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.queryByDisplayValue(/\/p\/stable-link\/$/)).toBeNull();
  });

  it('keeps the online toggle and custom link out while showing Organization access', async () => {
    const organizationAccess = showPageAccess({
      mode: 'organization',
      ownership_status: 'unchanged',
      instance_id: 'inst-1',
      organization_id: 'org-1',
      policy_organization_id: 'org-1',
      can_use: true,
      can_manage: true,
      can_publish_public: true,
    });
    const pending = new Promise(() => undefined);
    permissionsApi.getPermissions.mockReturnValue(pending);
    permissionsApi.getResourceAccess.mockReturnValue(pending);
    api.getShowPageAccess.mockResolvedValue(organizationAccess);
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'private',
      active_url: '/show/ses-1/',
      share_id: 'stable-link',
      url_available: true,
      offline: false,
      title: null,
    });
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'private',
        share_id: 'stable-link',
        revision: 0,
        normalized_emails: [],
      },
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl
          sessionId="ses-1"
          initialAccess={organizationAccess}
          canManageInstance
        />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    expect(await screen.findByRole('button', { name: 'Access: Private' })).toBeTruthy();
    expect(screen.getByText('Organization access')).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'Custom link' })).toBeNull();
    expect(screen.queryByText('Page online')).toBeNull();
    expect(api.setShowPageAvailability).not.toHaveBeenCalled();
  });

  it('does not mount Organization access for a normal Personal Avibe', async () => {
    const personalAccess = showPageAccess({
      mode: 'personal',
      ownership_status: 'unchanged',
      instance_id: null,
      organization_id: null,
      policy_organization_id: null,
      can_use: true,
      can_manage: true,
      can_publish_public: true,
    });
    api.getShowPageAccess.mockResolvedValue(personalAccess);
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'private',
      active_url: '/show/ses-1/',
      share_id: null,
      url_available: true,
      offline: false,
      title: null,
    });
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'private',
        share_id: null,
        revision: 0,
        normalized_emails: [],
      },
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl
          sessionId="ses-1"
          initialAccess={personalAccess}
          canManageInstance
        />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    expect(await screen.findByRole('button', { name: 'Access: Private' })).toBeTruthy();
    expect(screen.queryByText('Organization access')).toBeNull();
    expect(permissionsApi.getPermissions).not.toHaveBeenCalled();
    expect(permissionsApi.getResourceAccess).not.toHaveBeenCalled();
  });

  it('shows a Personal ownership conflict through the Workspace control', async () => {
    const conflict = showPageAccess({
      mode: 'personal',
      ownership_status: 'conflict',
      instance_id: 'inst-1',
      organization_id: null,
      policy_organization_id: 'org-other',
      can_use: true,
      can_manage: true,
      can_publish_public: false,
    });
    api.getShowPageAccess.mockResolvedValue(conflict);
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
        <ShowPageShareControl sessionId="ses-1" initialAccess={conflict} canManageInstance />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    expect(await screen.findByText('This Show Page is bound to a different ownership domain. Access remains private until the conflict is resolved.')).toBeTruthy();
    expect(screen.queryByText('This Show Page belongs to a Personal Avibe.')).toBeNull();
    expect(permissionsApi.getPermissions).not.toHaveBeenCalled();
    expect(permissionsApi.getResourceAccess).not.toHaveBeenCalled();
  });

  it('keeps explicit custom-link saving while online and Organization controls stay out', async () => {
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: true,
      can_publish_public: true,
    }));
    api.ensureShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'public',
      active_url: '/p/stable-link/',
      share_id: 'stable-link',
      url_available: true,
      offline: false,
      title: null,
    });
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'public',
        share_id: 'stable-link',
        revision: 0,
        normalized_emails: [],
      },
    });
    api.applyShowAccess.mockResolvedValue({
      status: 'applied',
      show_access: {
        page_id: 'ses-1',
        access_mode: 'public',
        share_id: 'new-link',
        revision: 1,
        normalized_emails: [],
      },
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" canManageInstance />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    expect(await screen.findByRole('button', { name: 'Access: Fully public' })).toBeTruthy();
    const customLink = screen.getByRole('textbox', { name: 'Custom link' });
    expect((customLink as HTMLInputElement).value).toBe('stable-link');
    expect(screen.getByText('Custom link')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    fireEvent.change(customLink, { target: { value: 'new-link' } });
    fireEvent.blur(customLink);

    expect(api.applyShowAccess).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(api.applyShowAccess).toHaveBeenCalledWith('ses-1', {
        expected_revision: 0,
        target_access_mode: 'public',
        target_share_id: 'new-link',
        target_emails: [],
      });
    });
    expect(screen.queryByText('Organization access')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
    expect(api.setShowPageAvailability).not.toHaveBeenCalled();
  });

  it('shows the loading state while a first access read is pending', async () => {
    // The app-window caller has no access yet: while the access read is in
    // flight the popover must show the loading row, not the load-error text.
    let release: (() => void) | null = null;
    api.getShowPageAccess.mockImplementation(
      () => new Promise((resolve) => {
        release = () => resolve(showPageAccess({
          can_use: false,
          can_manage: true,
          can_publish_public: false,
        }));
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
