/** @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { showDockId } from '../../context/dockDoc';
import { openNativeExternal } from '../../lib/native';
import type { ShowPageAccess } from '../../lib/showPageAccess';
import { ShowPageShareControl } from './ShowPageShareControl';
import { ShowPageAnnotationHost } from './ShowPageAnnotationHost';
import { NATIVE_SHOW_SHARE_OPEN_EVENT, NativeShowShareEntry } from '../apps/NativeShowShareEntry';

const permissionsApi = vi.hoisted(() => ({
  getPermissions: vi.fn(),
  getResourceAccess: vi.fn(),
  updateResourceAccess: vi.fn(),
}));
const native = vi.hoisted(() => ({ shell: false }));
const platform = vi.hoisted(() => ({
  ios: false,
  safari: false,
  standalone: false,
}));

vi.mock('../../lib/native', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/native')>()),
  isNativeShell: () => native.shell,
  openNativeExternal: vi.fn(async () => true),
}));
vi.mock('../../lib/platform', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/platform')>()),
  isIosDevice: () => platform.ios,
  isRealMobileSafari: () => platform.ios && platform.safari,
  isRealSafari: () => platform.safari,
  isStandalonePwa: () => platform.standalone,
}));

vi.mock('@/features/permissions/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/permissions/api')>()),
  getPermissions: permissionsApi.getPermissions,
  getResourceAccess: permissionsApi.getResourceAccess,
  updateResourceAccess: permissionsApi.updateResourceAccess,
}));

const api = {
  getShowPage: vi.fn(),
  ensureShowPage: vi.fn(),
  getShowPageAccess: vi.fn(),
  getShowAccessSettings: vi.fn(),
  applyShowAccess: vi.fn(),
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

const dock = vi.hoisted(() => ({
  isDocked: vi.fn(() => false),
  isPinned: vi.fn(() => false),
  dock: vi.fn(),
  pin: vi.fn(),
  undock: vi.fn(),
}));

vi.mock('../../context/DockContext', () => ({ useDock: () => dock }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_use_show_pages: true, can_manage_instance: true, can_chat: true } }),
}));

const toast = vi.hoisted(() => vi.fn());
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: toast }) }));

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
  dock.isDocked.mockReturnValue(false);
  dock.isPinned.mockReturnValue(false);
  native.shell = false;
  platform.ios = false;
  platform.safari = false;
  platform.standalone = false;
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

describe('ShowPageShareControl installation guidance', () => {
  const payload = {
    session_id: 'ses-1',
    visibility: 'private',
    active_url: '/show/ses-1/',
    share_id: null,
    url_available: true,
    offline: false,
    title: null,
  };

  const renderOpenControl = async () => {
    api.getShowPage.mockResolvedValue(payload);
    api.getShowPageAccess.mockResolvedValue(showPageAccess());
    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl
          sessionId="ses-1"
          initialAccess={showPageAccess()}
        />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    await screen.findByDisplayValue(/\/show\/ses-1\//);
  };

  it('keeps the system-entry guidance in the native shell and offers Safari handoff', async () => {
    platform.ios = true;
    platform.safari = true;
    await renderOpenControl();
    expect(await screen.findByText('Add to Home Screen')).toBeTruthy();
    cleanup();

    native.shell = true;
    await renderOpenControl();
    expect(await screen.findByText('Add to Home Screen or Dock')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open in browser' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'Copy link' })).toHaveLength(2);
    expect(screen.getByText(/default browser.*not Safari/i)).toBeTruthy();
    const showUrl = (screen.getByDisplayValue(/\/show\/ses-1\//) as HTMLInputElement).value;
    fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }));
    expect(openNativeExternal).toHaveBeenCalledWith(showUrl);
  });

  it('shows macOS Safari Dock guidance without using the Avibe Dock pin control', async () => {
    platform.ios = false;
    platform.safari = true;
    await renderOpenControl();

    expect(await screen.findByText('Add to Dock')).toBeTruthy();
    expect(screen.getByText(/Open this Show Page.*Share.*File.*Add to Dock/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open Show Page' }).getAttribute('href')).toContain('/show/ses-1/');
    expect(screen.getByRole('link', { name: 'Open Show Page' }).getAttribute('target')).toBe('_blank');
    expect(screen.queryByRole('switch', { name: 'Pin to Dock' })).toBeNull();
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
    api.getShowPage.mockResolvedValue({
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
      expect(api.getShowPage).toHaveBeenCalledWith('ses-1');
    });
    await waitFor(() => {
      expect((screen.getByDisplayValue(/\/show\/ses-1\//) as HTMLInputElement).value).toContain('/show/ses-1/');
    });
  });

  it('gives an Organization page one sharing axis and no Resource policy block', async () => {
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
    api.getShowPage.mockResolvedValue({
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
    expect(api.getShowAccessSettings).toHaveBeenCalledWith('ses-1');
    // The Organization axis and its Resource sync/ACK status are gone.
    expect(screen.queryByText('Organization access')).toBeNull();
    expect(screen.queryByText('Loading Organization access…')).toBeNull();
    expect(screen.queryByText(/has not acknowledged the latest policy/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();
    expect(permissionsApi.getResourceAccess).not.toHaveBeenCalled();
    expect(permissionsApi.updateResourceAccess).not.toHaveBeenCalled();
    // Private needs no audience, so not even the directory is read.
    expect(permissionsApi.getPermissions).not.toHaveBeenCalled();
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
        expect(api.getShowPage).not.toHaveBeenCalled();
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
    api.getShowPage.mockResolvedValue({
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
      expect(api.getShowPage).toHaveBeenCalledTimes(1);
    });
  });

  it('presents the Limited guest-admission link', async () => {
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: false,
      can_publish_public: false,
    }));
    api.getShowPage.mockResolvedValue({
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
    api.getShowPage.mockResolvedValue({
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

    await waitFor(() => expect(api.getShowPage).toHaveBeenCalledWith('ses-1'));
    expect((screen.getByRole('button', { name: 'Open' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Copy link' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.queryByDisplayValue(/\/p\/stable-link\/$/)).toBeNull();
  });

  it('shares an Organization page through the same Limited audience as any other', async () => {
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
    permissionsApi.getPermissions.mockResolvedValue({
      ok: true,
      source: 'live',
      offline: false,
      cached_at: null,
      projection: {
        schema_version: 1,
        instance: {
          id: 'inst-1',
          organization: { id: 'org-1', name: 'Acme' },
          access_mode: 'allowlist',
          permission_authority: 'cloud',
          local_mutation_allowed: false,
          authorization_revision: 2,
        },
        capabilities: [],
        access: { owner: { email: null, role: 'owner' }, entries: [] },
        directory: {
          members: [],
          groups: [{ id: 'grp-eng', name: 'Engineering', archived_at: null }],
        },
        projects: [],
        policy_sync: {
          status: 'in_sync',
          projects: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
          resources: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
        },
      },
    });
    api.getShowPageAccess.mockResolvedValue(organizationAccess);
    api.getShowPage.mockResolvedValue({
      session_id: 'ses-1',
      visibility: 'limited',
      active_url: '/p/stable-link/',
      share_id: 'stable-link',
      url_available: true,
      offline: false,
      title: null,
    });
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'limited',
        share_id: 'stable-link',
        revision: 0,
        normalized_emails: ['guest@example.com'],
        access_entries: [
          { kind: 'group', value: 'grp-eng' },
          { kind: 'email', value: 'guest@example.com' },
        ],
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

    expect(await screen.findByRole('button', { name: 'Access: Limited' })).toBeTruthy();
    // Custom-link editing is no longer withheld from Organization pages.
    expect((screen.getByRole('textbox', { name: 'Custom link' }) as HTMLInputElement).value).toBe(
      'stable-link',
    );
    // Group, email, and the Organization switch are peers in one audience list.
    expect(await screen.findByRole('switch', { name: 'This Organization' })).toBeTruthy();
    expect(screen.getByText('Engineering')).toBeTruthy();
    expect(screen.getByText('guest@example.com')).toBeTruthy();
    expect(screen.queryByText('Organization access')).toBeNull();
    expect(permissionsApi.getResourceAccess).not.toHaveBeenCalled();
    expect(permissionsApi.updateResourceAccess).not.toHaveBeenCalled();
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
    api.getShowPage.mockResolvedValue({
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

  it('does not resurrect an ownership-domain block on a Personal conflict', async () => {
    // Ownership conflict used to open the Resource-policy control. Sharing no
    // longer depends on ownership, so the conflict adds no second block at all.
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
    api.getShowPage.mockResolvedValue({
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

    await waitFor(() => expect(api.getShowPage).toHaveBeenCalledWith('ses-1'));
    expect(screen.queryByText(/bound to a different ownership domain/)).toBeNull();
    expect(screen.queryByText('Organization access')).toBeNull();
    expect(permissionsApi.getPermissions).not.toHaveBeenCalled();
    expect(permissionsApi.getResourceAccess).not.toHaveBeenCalled();
    expect(permissionsApi.updateResourceAccess).not.toHaveBeenCalled();
  });

  it('keeps explicit custom-link saving while online and Organization controls stay out', async () => {
    api.getShowPageAccess.mockResolvedValue(showPageAccess({
      can_use: true,
      can_manage: true,
      can_publish_public: true,
    }));
    api.getShowPage.mockResolvedValue({
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
        target_entries: [],
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

describe('ShowPageShareControl request surface', () => {
  // The property: opening this panel READS a Show Page, it never creates one.
  // `POST .../ensure` reports the first-creation edge exactly once, and only the
  // caller that sends the "visualize this session" prompt may consume it — so
  // this panel must stay off that endpoint no matter which branch it opens
  // through. Asserted as a subset check against the declared read-only surface
  // instead of a list of forbidden calls: an API this component starts calling
  // later fails here until someone declares it read-only, and one that is not
  // even on the mocked surface fails louder still.
  const READ_ONLY_ON_OPEN = new Set([
    'getShowPage',
    'getShowPageAccess',
    'getShowAccessSettings',
  ]);

  const mutatingCallsSoFar = () => Object.entries(api)
    .filter(([name, fn]) => !READ_ONLY_ON_OPEN.has(name) && fn.mock.calls.length > 0)
    .map(([name]) => name);

  const page = {
    session_id: 'ses-1',
    visibility: 'private',
    active_url: '/show/ses-1/',
    share_id: null,
    url_available: true,
    offline: false,
    title: null,
  };

  it.each([
    // Both mount shapes: ChatPage passes access it already has, AppWindow does
    // not and makes the panel resolve access first. They open through different
    // branches, so the property is asserted on each.
    ['with access already resolved', true],
    ['resolving access on open', false],
  ])('reaches only read-only APIs when opening %s', async (_label, seeded) => {
    const access = showPageAccess({ can_use: true, can_manage: true, can_publish_public: true });
    api.getShowPageAccess.mockResolvedValue(access);
    api.getShowPage.mockResolvedValue(page);
    api.getShowAccessSettings.mockResolvedValue({
      show_access: {
        page_id: 'ses-1',
        access_mode: 'private',
        share_id: null,
        revision: 1,
        normalized_emails: [],
      },
    });

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" initialAccess={seeded ? access : null} />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => expect(api.getShowPage).toHaveBeenCalledWith('ses-1'));
    await waitFor(() => {
      expect((screen.getByDisplayValue(/\/show\/ses-1\//) as HTMLInputElement).value).toContain('/show/ses-1/');
    });
    expect(mutatingCallsSoFar()).toEqual([]);
  });

  it('reaches only read-only APIs when the page it wants does not exist', async () => {
    // The case that used to CREATE one: a framed session whose page row is gone.
    // The read fails and the panel stays empty; it must not fall back to ensure.
    api.getShowPageAccess.mockResolvedValue(showPageAccess({ can_use: true, can_manage: true }));
    api.getShowPage.mockRejectedValue(new Error('show_page_not_found'));

    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));

    await waitFor(() => expect(api.getShowPage).toHaveBeenCalledWith('ses-1'));
    expect(mutatingCallsSoFar()).toEqual([]);
  });
});

// PERMISSIONS-021. `/api/dock/pins` is an editor-tier route, so the Dock row
// follows page use, not instance management. PM verified both verbs over signed
// HTTP: an Editor pins and unpins with a persisted readback, a Viewer is denied.
describe('PERMISSIONS-021 ShowPageShareControl Dock pin authority', () => {
  const page = {
    session_id: 'ses-1',
    visibility: 'private',
    active_url: '/show/ses-1/',
    share_id: null,
    url_available: true,
    offline: false,
    title: 'Release notes',
  };

  const renderShare = (props: { canPinToDock?: boolean; canUse?: boolean } = {}) => {
    const access = showPageAccess({ can_use: props.canUse !== false, can_manage: false });
    api.getShowPageAccess.mockResolvedValue(access);
    api.getShowPage.mockResolvedValue(page);
    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl
          sessionId="ses-1"
          initialAccess={access}
          canPinToDock={props.canPinToDock ?? true}
        />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
  };

  it('pins a page for a user who may use it but may not manage the instance', async () => {
    renderShare();

    const toggle = await screen.findByRole('switch', { name: 'Pin to Dock' });
    fireEvent.click(toggle);

    expect(dock.pin).toHaveBeenCalledWith('ses-1');
    expect(dock.undock).not.toHaveBeenCalled();
  });

  it('unpins the same page and reports it as docked', async () => {
    dock.isDocked.mockReturnValue(true);
    dock.isPinned.mockReturnValue(true);
    renderShare();

    expect(await screen.findByText(/Pinned — “Release notes” is in your Dock/)).toBeTruthy();
    fireEvent.click(screen.getByRole('switch', { name: 'Pin to Dock' }));

    expect(dock.undock).toHaveBeenCalledWith(showDockId('ses-1'));
    expect(dock.pin).not.toHaveBeenCalled();
  });

  it('offers no Dock control to a role that cannot reach the pins route', async () => {
    renderShare({ canPinToDock: false });

    // The share link still loads, so this is the Dock row being withheld, not an
    // unreadable page.
    await waitFor(() => expect(screen.getByDisplayValue(/\/show\/ses-1\//)).toBeTruthy());
    expect(screen.queryByRole('switch', { name: 'Pin to Dock' })).toBeNull();
    expect(dock.pin).not.toHaveBeenCalled();
  });

  it('withholds the Dock control when page use itself is revoked', async () => {
    renderShare({ canUse: false });

    await waitFor(() => expect(api.getShowPageAccess).toHaveBeenCalled());
    expect(screen.queryByRole('switch', { name: 'Pin to Dock' })).toBeNull();
    expect(api.getShowPage).not.toHaveBeenCalled();
  });
});

describe('ShowPageShareControl native share and export', () => {
  const payload = {
    session_id: 'ses-1',
    visibility: 'private',
    active_url: '/show/ses-1/',
    share_id: null,
    url_available: true,
    offline: false,
    title: null,
  };
  const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const fullCapabilities = {
    version: 1,
    inbound_kinds: [],
    outbound_kinds: ['link', 'pdf', 'image'],
    max_bytes: { pdf: 52428800, image: 52428800, link: 32768 },
    max_total_bytes: 52428800,
    max_attachments: 1,
    supports_pending: false,
  };

  type Present = (params: { operationId: string; kind: string; sessionId: string }) => unknown;

  const installShell = ({
    shareDraft = true,
    capabilities = fullCapabilities as unknown,
    present = (async (params) => ({ ...params, status: 'completed' })) as Present,
  } = {}) => {
    native.shell = true;
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'share.capabilities') return capabilities;
      if (method === 'share.present') {
        return present(params as { operationId: string; kind: string; sessionId: string });
      }
      throw { code: 'unknown_method', message: method };
    });
    Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: {
        contract: 1,
        platform: 'ios',
        shellVersion: '1.0 (1)',
        capabilities: shareDraft ? { app: 1, 'c1.share_draft': 1 } : { app: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
    });
    return call;
  };

  const mountShowFrame = (sessionId = 'ses-1') => {
    const frame = document.createElement('iframe');
    frame.dataset.avibeShowSession = sessionId;
    frame.getBoundingClientRect = () => ({
      x: 0, y: 0, left: 0, top: 0, right: 320, bottom: 480, width: 320, height: 480, toJSON: () => ({}),
    });
    document.body.appendChild(frame);
    return frame;
  };

  const presentCalls = (call: ReturnType<typeof installShell>) =>
    call.mock.calls.filter(([method]) => method === 'share.present');

  const openControl = async (onOpenChange?: (open: boolean) => void) => {
    api.getShowPage.mockResolvedValue(payload);
    api.getShowPageAccess.mockResolvedValue(showPageAccess());
    render(
      <I18nextProvider i18n={i18n}>
        <ShowPageShareControl sessionId="ses-1" initialAccess={showPageAccess()} onOpenChange={onOpenChange} />
      </I18nextProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    await screen.findByDisplayValue(/\/show\/ses-1\//);
  };

  const expectNoWrites = () => {
    expect(api.ensureShowPage).not.toHaveBeenCalled();
    expect(api.applyShowAccess).not.toHaveBeenCalled();
    expect(api.setShowPageAvailability).not.toHaveBeenCalled();
    expect(permissionsApi.updateResourceAccess).not.toHaveBeenCalled();
  };

  afterEach(() => {
    Reflect.deleteProperty(window, 'avibeNative');
    document.querySelectorAll('iframe[data-avibe-show-session]').forEach((frame) => frame.remove());
  });

  it('exports only after the notice is accepted, with the share UI gone and the request bound to its Session', async () => {
    let overlaysAtCapture: number | null = null;
    const call = installShell({
      present: async (params) => {
        overlaysAtCapture = document.querySelectorAll('[role="dialog"], [data-radix-popper-content-wrapper]').length;
        return { ...params, status: 'completed' };
      },
    });
    mountShowFrame();
    await openControl();

    fireEvent.click(screen.getByRole('button', { name: 'Share PDF' }));
    expect(await screen.findByText(/Recipients can keep a copy that Avibe cannot revoke\./)).toBeTruthy();
    expect(screen.queryByText('Share this Show Page')).toBeNull();
    expect(call).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(presentCalls(call)).toHaveLength(1));
    const [, params] = presentCalls(call)[0];
    expect(Object.keys(params ?? {}).sort()).toEqual(['kind', 'operationId', 'sessionId']);
    expect(params).toMatchObject({ kind: 'pdf', sessionId: 'ses-1' });
    expect(params?.operationId).toMatch(uuidV4);
    expect(call.mock.calls[0][0]).toBe('share.capabilities');
    expect(overlaysAtCapture).toBe(0);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Share' })).toBe(document.activeElement));
    expect(toast).not.toHaveBeenCalled();
    expectNoWrites();
  });

  it('exports nothing when the notice is cancelled, and a cancelled share says nothing', async () => {
    const call = installShell({ present: async (params) => ({ ...params, status: 'cancelled' }) });
    mountShowFrame();
    await openControl();

    fireEvent.click(screen.getByRole('button', { name: 'Share image' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Share' })).toBe(document.activeElement));
    expect(call).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Share image' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(presentCalls(call)).toHaveLength(1));
    expect(presentCalls(call)[0][1]).toMatchObject({ kind: 'image', sessionId: 'ses-1' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Share' })).toBe(document.activeElement));
    expect(toast).not.toHaveBeenCalled();
  });

  it('refuses an export the shell does not offer and asks for an update', async () => {
    const call = installShell({ capabilities: { ...fullCapabilities, outbound_kinds: ['link'] } });
    mountShowFrame();
    await openControl();

    fireEvent.click(screen.getByRole('button', { name: 'Share PDF' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'This version of Avibe can only share links. Update Avibe to export a PDF or image.',
      'error',
    ));
    expect(presentCalls(call)).toHaveLength(0);
  });

  it.each([
    ['failed', { status: 'failed', error: { code: 'capture_failed' } }, 'Couldn’t share. Try again.'],
    ['expired', { status: 'expired', error: { code: 'deadline' } }, 'This share has expired. Share again.'],
    ['a malformed', { status: 'done' }, 'Couldn’t share. Try again.'],
  ])('reports a %s result with a retryable error', async (_label, result, message) => {
    const call = installShell({ present: async (params) => ({ ...params, ...result }) });
    mountShowFrame();
    await openControl();

    fireEvent.click(screen.getByRole('button', { name: 'Share image' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(message, 'error'));

    // The error leaves the action ready for a new, explicit attempt.
    fireEvent.click(screen.getByRole('button', { name: 'Share' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Share image' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(presentCalls(call)).toHaveLength(2));
    expect(presentCalls(call)[1][1]?.operationId).not.toBe(presentCalls(call)[0][1]?.operationId);
  });

  it('shares the link through the shell once while a share is open', async () => {
    let finish: (value: unknown) => void = () => undefined;
    const call = installShell({
      present: (params) => new Promise((resolve) => {
        finish = () => resolve({ ...params, status: 'completed' });
      }),
    });
    await openControl();

    const shareLink = screen.getByRole('button', { name: 'Share link' });
    fireEvent.click(shareLink);
    fireEvent.click(shareLink);
    await waitFor(() => expect(presentCalls(call)).toHaveLength(1));
    expect(presentCalls(call)[0][1]).toMatchObject({ kind: 'link', sessionId: 'ses-1' });
    expect((shareLink as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(shareLink);

    await act(async () => finish(undefined));
    await waitFor(() => expect((shareLink as HTMLButtonElement).disabled).toBe(false));
    expect(presentCalls(call)).toHaveLength(1);
    expect(toast).not.toHaveBeenCalled();
    expectNoWrites();
  });

  it('offers export only for a Show the user sees now', async () => {
    installShell();
    mountShowFrame('ses-other');
    await openControl();

    expect(screen.getByRole('button', { name: 'Share link' })).toBeTruthy();
    expect(screen.queryByText('Export visible content')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Share PDF' })).toBeNull();
  });

  it('offers no export while the Show is under annotation', async () => {
    installShell();
    mountShowFrame().style.pointerEvents = 'none';
    await openControl();

    expect(screen.getByRole('button', { name: 'Share link' })).toBeTruthy();
    expect(screen.queryByText('Export visible content')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Share PDF' })).toBeNull();
  });

  it('retains the pre-open visibility sample when the host shields pointer events', async () => {
    installShell();
    const frame = mountShowFrame();
    await openControl((open) => {
      // Hosts apply this during their React render, after the open callback.
      queueMicrotask(() => { frame.style.pointerEvents = open ? 'none' : 'auto'; });
    });
    await waitFor(() => expect(frame.style.pointerEvents).toBe('none'));
    expect(screen.getByRole('button', { name: 'Share PDF' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Share image' })).toBeTruthy();
  });

  it('explains the update in an App that can only share links, without exporting', async () => {
    const call = installShell({ shareDraft: false });
    mountShowFrame();
    await openControl();

    expect(screen.getByText(
      'This version of Avibe can only share links. Update Avibe to export a PDF or image.',
    )).toBeTruthy();
    const pdf = screen.getByRole('button', { name: 'Share PDF' }) as HTMLButtonElement;
    expect(pdf.disabled).toBe(true);
    fireEvent.click(pdf);
    expect(screen.queryByText(/Recipients can keep a copy/)).toBeNull();
    expect(call).not.toHaveBeenCalled();
  });

  it('adds no export to a browser', async () => {
    mountShowFrame();
    await openControl();

    expect(screen.queryByText('Export visible content')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Share PDF' })).toBeNull();
  });

  describe('in the desktop app', () => {
    const DESKTOP = [
      '__AVIBE_DESKTOP_SHELL__', '__AVIBE_NATIVE_APP_WINDOW__', '__AVIBE_DESKTOP_SHOW_SHARE__',
      '__AVIBE_DESKTOP_SHOW_EXPORT__', '__TAURI_INTERNALS__',
    ];
    const define = (name: string, value: unknown) => Object.defineProperty(window, name, { configurable: true, value });
    /** The markers the shell injects: the Workbench shares links; a native Show window also exports. */
    const installDesktop = (surface: 'workbench' | 'show-window' | 'none') => {
      const invoke = vi.fn(async (_command: string, args?: { request?: Record<string, unknown> }) => {
        const { method, ...params } = args?.request ?? {};
        return method === 'share.capabilities' ? fullCapabilities : { ...params, status: 'cancelled' };
      });
      define(surface === 'show-window' ? '__AVIBE_NATIVE_APP_WINDOW__' : '__AVIBE_DESKTOP_SHELL__', true);
      if (surface !== 'none') define('__AVIBE_DESKTOP_SHOW_SHARE__', 1);
      if (surface === 'show-window') define('__AVIBE_DESKTOP_SHOW_EXPORT__', 1);
      define('__TAURI_INTERNALS__', { invoke });
      return invoke;
    };
    afterEach(() => DESKTOP.forEach((name) => Reflect.deleteProperty(window, name)));

    it('offers export when the Show frame becomes visible after the popover opens', async () => {
      const invoke = installDesktop('show-window');
      await openControl();
      expect(screen.queryByRole('button', { name: 'Share PDF' })).toBeNull();

      const frame = mountShowFrame();
      // The loading body mounts before it becomes eligible for capture.
      frame.style.pointerEvents = 'none';
      await act(async () => { fireEvent.load(frame); });
      expect(screen.queryByRole('button', { name: 'Share PDF' })).toBeNull();
      frame.style.pointerEvents = 'auto';

      expect(await screen.findByRole('button', { name: 'Share PDF' })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Share image' })).toBeTruthy();
      expect(screen.getByText('Share this Show Page')).toBeTruthy();
      expect(invoke).not.toHaveBeenCalled();
      expectNoWrites();
    });

    it('shares only the link from the Workbench', async () => {
      const invoke = installDesktop('workbench');
      mountShowFrame();
      await openControl();

      expect(screen.queryByText('Export visible content')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Share PDF' })).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
      await waitFor(() => expect(invoke).toHaveBeenCalledWith('desktop_show_share', {
        request: expect.objectContaining({ method: 'share.present', kind: 'link', sessionId: 'ses-1' }),
      }));
      expect(toast).not.toHaveBeenCalled();
      expectNoWrites();
    });

    it('exports from a native Show window through the desktop share command', async () => {
      const invoke = installDesktop('show-window');
      mountShowFrame();
      await openControl();

      expect(screen.queryByText(/Update Avibe/)).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Share image' }));
      fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
      await waitFor(() => expect(invoke).toHaveBeenCalledWith('desktop_show_share', {
        request: expect.objectContaining({ method: 'share.present', kind: 'image', sessionId: 'ses-1' }),
      }));
      await waitFor(() => expect(screen.getByRole('button', { name: 'Share' })).toBe(document.activeElement));
      expect(toast).not.toHaveBeenCalled();
      expectNoWrites();
    });

    it('keeps copying the link alone where the desktop app cannot share', async () => {
      const invoke = installDesktop('none');
      mountShowFrame();
      await openControl();

      expect(screen.getByRole('button', { name: 'Copy link' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Share link' })).toBeNull();
      expect(screen.queryByText('Export visible content')).toBeNull();
      expect(invoke).not.toHaveBeenCalled();
    });
  });
});

// ui/design: show-pages/desktop-native-entry/titlebar-*
describe('the native Show window share entry', () => {
  const pressTitleBarButton = () => act(() => { window.dispatchEvent(new CustomEvent(NATIVE_SHOW_SHARE_OPEN_EVENT)); });
  beforeEach(() => window.history.replaceState(null, '', '/apps/show/ses-1?standalone=1'));
  afterEach(() => window.history.replaceState(null, '', '/'));
  const mount = () => {
    api.getShowPage.mockResolvedValue({
      session_id: 'ses-1', visibility: 'private', active_url: '/show/ses-1/', share_id: null,
      url_available: true, offline: false, title: null,
    });
    api.getShowPageAccess.mockResolvedValue(showPageAccess());
    return render(
      <I18nextProvider i18n={i18n}>
        <ShowPageAnnotationHost src={null}><div className="relative"><NativeShowShareEntry sessionId="ses-1" /></div></ShowPageAnnotationHost>
      </I18nextProvider>,
    );
  };

  it('opens the share popover on the title-bar press and closes it on the next', async () => {
    mount();
    expect(screen.queryByText('Share this Show Page')).toBeNull();
    pressTitleBarButton();
    expect(await screen.findByText('Share this Show Page')).toBeTruthy();
    expect(document.querySelector('[data-share-shield]')).toBeTruthy();
    pressTitleBarButton();
    await waitFor(() => expect(screen.queryByText('Share this Show Page')).toBeNull());
    expect(document.querySelector('[data-share-shield]')).toBeNull();
  });

  it('closes on Escape and keeps its hidden anchor out of reach', async () => {
    mount();
    const anchor = document.querySelector('[data-native-share-anchor] button') as HTMLButtonElement;
    expect(anchor.tabIndex).toBe(-1);
    expect(anchor.getAttribute('aria-hidden')).toBe('true');
    expect(anchor.className).toContain('opacity-0');
    pressTitleBarButton();
    expect(await screen.findByText('Share this Show Page')).toBeTruthy();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByText('Share this Show Page')).toBeNull());
    expect(document.activeElement).not.toBe(anchor);
  });

  // Review 45533723, third P2: off its route, the window's entry does nothing; back on it, it works.
  it('answers the title-bar press only on its own Show route', async () => {
    window.history.replaceState(null, '', '/apps/show/ses-1?standalone=1');
    mount();
    window.history.pushState(null, '', '/chat/ses-2');
    pressTitleBarButton();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText('Share this Show Page')).toBeNull();
    window.history.pushState(null, '', '/apps/show/ses-1?standalone=1');
    pressTitleBarButton();
    expect(await screen.findByText('Share this Show Page')).toBeTruthy();
    window.history.replaceState(null, '', '/');
  });

  it('shares this window\'s Show, whoever dispatches the event', async () => {
    mount();
    pressTitleBarButton();
    expect(await screen.findByDisplayValue(/\/show\/ses-1\//)).toBeTruthy();
    expect(api.getShowPage).toHaveBeenCalledWith('ses-1');
  });
});
