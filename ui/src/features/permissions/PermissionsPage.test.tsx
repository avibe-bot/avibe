/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { PermissionsApiError } from './api';
import { PermissionsPage } from './PermissionsPage';
import { requiresAccessNarrowing } from './policy';
import type {
  AuthorizedUsersWriteResponse,
  PermissionsResponse,
  ProjectAccessWriteResponse,
} from './types';

const api = vi.hoisted(() => ({
  getPermissions: vi.fn(),
  replaceAuthorizedUsers: vi.fn(),
  updateProjectAccess: vi.fn(),
}));

vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  getPermissions: api.getPermissions,
  replaceAuthorizedUsers: api.replaceAuthorizedUsers,
  updateProjectAccess: api.updateProjectAccess,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const response = (overrides: Partial<PermissionsResponse> = {}): PermissionsResponse => ({
  ok: true,
  source: 'live',
  offline: false,
  cached_at: null,
  projection: {
    schema_version: 1,
    instance: {
      id: 'inst-123',
      name: 'max-incus-1',
      public_url: 'https://max-incus-1-app.avibe.bot',
      organization: { id: 'org-1', name: 'CoinSummer' },
      access_mode: 'allowlist',
      permission_authority: 'instance',
      local_mutation_allowed: true,
      authorization_revision: 4,
    },
    capabilities: ['instance.permissions.read', 'instance.permissions.mutate'],
    access: {
      owner: { email: 'owner@example.com', role: 'owner' },
      entries: [],
    },
    directory: { members: [], groups: [] },
    projects: [{
      project_id: 'project-1',
      organization_id: 'org-1',
      display_name: 'Launch Plan',
      access: {
        mode: 'restricted',
        revision: 1,
        bindings: [{
          principal_kind: 'email',
          principal_value: 'viewer@example.com',
          access_role: 'viewer',
        }],
      },
      sync: {
        status: 'in_sync',
        desired_access_revision: 1,
        applied_access_revision: 1,
        last_synced_at: null,
      },
    }],
    policy_sync: {
      status: 'in_sync',
      projects: { active: 1, error: 0, offline: 0, applying: 0, in_sync: 1 },
      resources: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
    },
  },
  ...overrides,
});

function renderPage(canManage = true) {
  return render(
    <InstanceAuthorizationContext.Provider value={{
      remote: true,
      instanceKind: 'organization',
      instanceRole: canManage ? 'owner' : 'viewer',
      capabilities: canManage
        ? OWNER_INSTANCE_CAPABILITIES
        : { ...OWNER_INSTANCE_CAPABILITIES, can_manage_instance: false, is_instance_owner: false },
    }}>
      <PermissionsPage />
    </InstanceAuthorizationContext.Provider>,
  );
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

beforeEach(() => {
  api.getPermissions.mockReset();
  api.getPermissions.mockResolvedValue(response());
  api.replaceAuthorizedUsers.mockReset();
  api.updateProjectAccess.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('PermissionsPage state model', () => {
  it('uses the full settings content width instead of a page-specific max width', async () => {
    renderPage();

    const heading = await screen.findByRole('heading', { name: 'permissions.title' });
    const page = heading.closest('header')?.parentElement;

    expect(page).not.toBeNull();
    expect(page?.classList.contains('w-full')).toBe(true);
    expect(page?.classList.contains('mx-auto')).toBe(false);
    expect(Array.from(page?.classList ?? []).some((className) => className.startsWith('max-w-'))).toBe(false);
  });

  it('renders the current Avibe identity and Cloud handoff from the design contract', async () => {
    renderPage();

    const heading = await screen.findByRole('heading', { name: 'permissions.title' });
    expect(heading.previousElementSibling).toBeNull();
    expect(await screen.findByText('max-incus-1')).toBeTruthy();
    expect(screen.getByText('max-incus-1-app.avibe.bot')).toBeTruthy();
    expect(screen.getByText('CoinSummer')).toBeTruthy();
    expect(screen.getByText('permissions.currentInstance')).toBeTruthy();
    const cloudLink = screen.getByRole('link', { name: /permissions.actions.openCloud/ });
    expect(cloudLink.getAttribute('href')).toBe('https://avibe.bot');
    expect(cloudLink.getAttribute('target')).toBe('_blank');
    expect(screen.queryByText('inst-123')).toBeNull();
  });

  it('keeps legacy projections usable without inventing an Organization name', async () => {
    const legacy = response();
    delete legacy.projection.instance.name;
    delete legacy.projection.instance.public_url;
    delete legacy.projection.instance.organization;
    api.getPermissions.mockResolvedValue(legacy);

    renderPage();

    expect(await screen.findAllByText('permissions.currentInstance')).toHaveLength(2);
    expect(screen.queryByText('CoinSummer')).toBeNull();
    expect(screen.queryByText('inst-123')).toBeNull();
  });

  it('renders Cloud-owned policy as a visible read-only state', async () => {
    const cloud = response();
    cloud.projection.instance.permission_authority = 'cloud';
    cloud.projection.instance.local_mutation_allowed = false;
    api.getPermissions.mockResolvedValue(cloud);

    renderPage();

    expect(await screen.findByText('permissions.states.cloudTitle')).toBeTruthy();
    expect(screen.getByRole('link', { name: /permissions.actions.openCloud/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /permissions.actions.addAccess/ })).toBeNull();
  });

  it('keeps cached policy distinct and can recover it without a page reload', async () => {
    const cached = response({ source: 'cache', offline: true, cached_at: 123 });
    api.getPermissions
      .mockResolvedValueOnce(cached)
      .mockResolvedValueOnce(response());
    const user = userEvent.setup();

    renderPage();

    expect(await screen.findByText('permissions.states.offlineTitle')).toBeTruthy();
    expect(screen.getByText('permissions.access.emptyTitle')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /permissions.actions.addAccess/ })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'permissions.actions.refresh' }));

    await waitFor(() => {
      expect(screen.queryByText('permissions.states.offlineTitle')).toBeNull();
    });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'permissions.actions.addAccess' })).toBeTruthy();
  });

  it('describes public access instead of claiming an owner-only empty policy', async () => {
    const publicPolicy = response();
    publicPolicy.projection.instance.access_mode = 'public';
    api.getPermissions.mockResolvedValue(publicPolicy);

    renderPage();

    expect(await screen.findByText('permissions.access.publicTitle')).toBeTruthy();
    expect(screen.getByText('permissions.access.publicBody')).toBeTruthy();
    expect(screen.queryByText('permissions.access.emptyBody')).toBeNull();
  });

  it('keeps the public audience visible alongside explicit assignments', async () => {
    const publicPolicy = response();
    publicPolicy.projection.instance.access_mode = 'public';
    publicPolicy.projection.access.entries = [{
      kind: 'email',
      value: 'editor@example.com',
      role: 'editor',
    }];
    api.getPermissions.mockResolvedValue(publicPolicy);

    renderPage();

    expect(await screen.findByText('permissions.access.publicTitle')).toBeTruthy();
    expect(screen.getByText('permissions.access.publicAssignmentsBody')).toBeTruthy();
    expect(screen.getByText('editor@example.com')).toBeTruthy();
  });

  it('lets a Viewer read policy while keeping mutation controls absent', async () => {
    renderPage(false);

    expect(await screen.findByText('permissions.states.readOnlyTitle')).toBeTruthy();
    expect(screen.getByText('owner@example.com')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /permissions.actions.addAccess/ })).toBeNull();
  });

  it('gates every edit surface on the Backend mutation capability', async () => {
    const readOnly = response();
    readOnly.projection.capabilities = ['instance.permissions.read'];
    api.getPermissions.mockResolvedValue(readOnly);
    const user = userEvent.setup();

    renderPage();

    expect(await screen.findByText('permissions.states.readOnlyTitle')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /permissions.actions.addAccess/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /permissions.actions.editAccess/ })).toBeNull();
    await user.click(screen.getByRole('tab', { name: 'permissions.tabs.projects' }));
    expect(screen.queryByRole('button', { name: 'permissions.actions.manage' })).toBeNull();
  });

  it('ignores additive capabilities while honoring the known mutation capability', async () => {
    const extended = response();
    extended.projection.capabilities.push('instance.permissions.audit');
    api.getPermissions.mockResolvedValue(extended);

    renderPage();

    expect(await screen.findByRole('button', {
      name: 'permissions.actions.addAccess',
    })).toBeTruthy();
  });

  it('renders an API denial instead of an empty policy', async () => {
    api.getPermissions.mockRejectedValue(new PermissionsApiError(403, { error: 'instance_access_forbidden' }));

    renderPage(false);

    expect(await screen.findByText('permissions.states.deniedTitle')).toBeTruthy();
    expect(screen.queryByText('permissions.access.emptyTitle')).toBeNull();
  });

  it('does not misclassify a paired credential failure as user denial', async () => {
    api.getPermissions.mockRejectedValue(new PermissionsApiError(401, {
      error: 'invalid_device_secret',
    }));

    renderPage();

    expect(await screen.findByText('permissions.states.unavailableTitle')).toBeTruthy();
    expect(screen.queryByText('permissions.states.deniedTitle')).toBeNull();
  });

  it.each([
    [409, 'permissions_not_paired', 'permissions.states.unavailableTitle'],
    [409, 'permissions_pairing_changed', 'permissions.states.unavailableTitle'],
    [401, 'invalid_device_secret', 'permissions.states.unavailableTitle'],
    [403, 'instance_access_forbidden', 'permissions.states.deniedTitle'],
  ] as const)('clears the ready projection after authoritative %s/%s refresh failure', async (
    status,
    code,
    terminalTitle,
  ) => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockRejectedValueOnce(new PermissionsApiError(status, { error: code }));

    renderPage();
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('owner@example.com')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'permissions.actions.addAccess' })).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(screen.getByText(terminalTitle)).toBeTruthy();
    expect(screen.queryByText('owner@example.com')).toBeNull();
    expect(screen.queryByRole('button', { name: 'permissions.actions.addAccess' })).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);
  });

  it('refreshes a live applying policy until it converges', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    const inSync = response();
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockResolvedValueOnce(inSync);

    renderPage();
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('permissions.states.applyingTitle')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(api.getPermissions).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('permissions.states.applyingTitle')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);
  });

  it('keeps a mutation epoch when an older in-flight policy refresh arrives later', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    applying.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    const stale = response();
    stale.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    let resolvePoll: (value: PermissionsResponse) => void = () => undefined;
    const poll = new Promise<PermissionsResponse>((resolve) => {
      resolvePoll = resolve;
    });
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockReturnValueOnce(poll);
    api.replaceAuthorizedUsers
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 5,
        entries: [{ kind: 'email', value: 'viewer@example.com', role: 'editor' }],
      })
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 6,
        entries: [],
      });
    renderPage();
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
    fireEvent.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    await act(async () => { await Promise.resolve(); });
    expect(api.replaceAuthorizedUsers).toHaveBeenCalledWith(
      [{ kind: 'email', value: 'viewer@example.com', role: 'editor' }],
      4,
      'inst-123',
    );
    expect(screen.getByText('permissions.roles.editor')).toBeTruthy();

    await act(async () => {
      resolvePoll(stale);
      await Promise.resolve();
    });

    expect(screen.getByText('permissions.states.applyingTitle')).toBeTruthy();
    expect(screen.getByText('permissions.roles.editor')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.removeAccess' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', {
      name: 'permissions.actions.removeAccess',
    }));
    await act(async () => { await Promise.resolve(); });
    expect(api.replaceAuthorizedUsers.mock.calls[1]).toEqual([
      [],
      5,
      'inst-123',
    ]);
  });

  it('keeps the latest paired instance when recovery responses resolve out of order', async () => {
    vi.useFakeTimers();
    const cachedA = response({ source: 'cache', offline: true, cached_at: 123 });
    const staleA = response();
    staleA.projection.instance.name = 'stale-instance-a';
    staleA.projection.policy_sync.status = 'applying';
    staleA.projection.projects[0]!.sync.status = 'pending';
    const cachedB = response({ source: 'cache', offline: true, cached_at: 456 });
    cachedB.projection.instance.id = 'inst-b';
    cachedB.projection.instance.name = 'instance-b';
    cachedB.projection.instance.authorization_revision = 1;
    const staleRequest = deferred<PermissionsResponse>();
    const currentRequest = deferred<PermissionsResponse>();
    api.getPermissions
      .mockResolvedValueOnce(cachedA)
      .mockReturnValueOnce(staleRequest.promise)
      .mockReturnValueOnce(currentRequest.promise);

    renderPage();
    await act(async () => { await Promise.resolve(); });
    const refresh = screen.getByRole('button', { name: 'permissions.actions.refresh' });
    fireEvent.click(refresh);
    fireEvent.click(refresh);
    expect(api.getPermissions).toHaveBeenCalledTimes(3);

    await act(async () => {
      currentRequest.resolve(cachedB);
      await Promise.resolve();
    });
    expect(screen.getByText('instance-b')).toBeTruthy();
    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();

    await act(async () => {
      staleRequest.resolve(staleA);
      await Promise.resolve();
    });
    expect(screen.getByText('instance-b')).toBeTruthy();
    expect(screen.queryByText('stale-instance-a')).toBeNull();
    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();
    expect(screen.queryByText('permissions.states.applyingTitle')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(3);
  });

  it('uses the newest same-instance response when a conflict refresh is superseded', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    applying.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    const staleInstance = response();
    staleInstance.projection.instance.id = 'inst-b';
    staleInstance.projection.instance.name = 'stale-instance-b';
    const latest = response();
    latest.projection.instance.authorization_revision = 5;
    latest.projection.access.entries = applying.projection.access.entries;
    const conflictRefresh = deferred<PermissionsResponse>();
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockReturnValueOnce(conflictRefresh.promise)
      .mockResolvedValueOnce(latest);
    api.replaceAuthorizedUsers
      .mockRejectedValueOnce(new PermissionsApiError(409, {
        error: 'permission_revision_conflict',
        current_revision: 5,
      }))
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 6,
        entries: [{ kind: 'email', value: 'viewer@example.com', role: 'editor' }],
      });

    renderPage();
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
    fireEvent.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    await act(async () => { await Promise.resolve(); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(3);
    await act(async () => {
      conflictRefresh.resolve(staleInstance);
      await Promise.resolve();
    });

    expect(screen.getByText('permissions.states.conflictBody')).toBeTruthy();
    expect(screen.queryByText('permissions.states.conflictRefreshBody')).toBeNull();
    expect(screen.queryByText('stale-instance-b')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.retrySave' }));
    await act(async () => { await Promise.resolve(); });
    expect(api.replaceAuthorizedUsers.mock.calls[1]).toEqual([
      [{ kind: 'email', value: 'viewer@example.com', role: 'editor' }],
      5,
      'inst-123',
    ]);
  });

  it.each([
    'access edit',
    'Project edit',
    'access removal',
  ] as const)('rejects an instance A %s acknowledgement after refresh installs instance B', async (flow) => {
    vi.useFakeTimers();
    const instanceA = response();
    instanceA.projection.policy_sync.status = 'applying';
    instanceA.projection.projects[0]!.sync.status = 'pending';
    instanceA.projection.access.entries = [{
      kind: 'email',
      value: 'instance-a@example.com',
      role: 'viewer',
    }];
    const instanceB = response();
    instanceB.projection.instance.id = 'inst-b';
    instanceB.projection.instance.name = 'instance-b';
    instanceB.projection.instance.authorization_revision = 1;
    instanceB.projection.access.entries = [{
      kind: 'email',
      value: 'instance-b@example.com',
      role: 'viewer',
    }];
    instanceB.projection.projects[0] = {
      ...instanceB.projection.projects[0]!,
      display_name: 'Instance B Project',
      access: {
        ...instanceB.projection.projects[0]!.access,
        bindings: [{
          principal_kind: 'email',
          principal_value: 'instance-b@example.com',
          access_role: 'viewer',
        }],
      },
    };
    api.getPermissions
      .mockResolvedValueOnce(instanceA)
      .mockResolvedValueOnce(instanceB);
    const accessAcknowledgement = deferred<AuthorizedUsersWriteResponse>();
    const projectAcknowledgement = deferred<ProjectAccessWriteResponse>();
    if (flow === 'Project edit') {
      api.updateProjectAccess.mockReturnValueOnce(projectAcknowledgement.promise);
    } else {
      api.replaceAuthorizedUsers.mockReturnValueOnce(accessAcknowledgement.promise);
    }
    renderPage();
    await act(async () => { await Promise.resolve(); });

    if (flow === 'access edit') {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
      fireEvent.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    } else if (flow === 'Project edit') {
      fireEvent.click(screen.getByRole('tab', { name: 'permissions.tabs.projects' }));
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
      fireEvent.change(screen.getByLabelText('permissions.fields.role'), {
        target: { value: 'editor' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    } else {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.removeAccess' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {
        name: 'permissions.actions.removeAccess',
      }));
    }
    await act(async () => { await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByText('instance-b')).toBeTruthy();

    await act(async () => {
      if (flow === 'Project edit') {
        projectAcknowledgement.resolve({
          ok: true,
          instance_id: 'inst-123',
          authorization_revision: 5,
          project: {
            ...instanceA.projection.projects[0]!,
            display_name: 'Instance A Acknowledgement',
          },
        });
      } else {
        accessAcknowledgement.resolve({
          ok: true,
          instance_id: 'inst-123',
          authorization_revision: 5,
          entries: flow === 'access edit' ? [{
            kind: 'email',
            value: 'instance-a@example.com',
            role: 'editor',
          }] : [],
        });
      }
      await Promise.resolve();
    });

    expect(screen.getByText('instance-b')).toBeTruthy();
    if (flow === 'Project edit') {
      expect(screen.getByText('Instance B Project')).toBeTruthy();
      expect(screen.queryByText('Instance A Acknowledgement')).toBeNull();
    } else {
      expect(screen.getByText('instance-b@example.com')).toBeTruthy();
      expect(screen.queryByText('instance-a@example.com')).toBeNull();
    }
  });

  it.each([
    ['a different instance', 'inst-other', 5],
    ['an older revision', 'inst-123', 3],
  ] as const)('rejects an access acknowledgement bound to %s', async (
    _condition,
    acknowledgementInstanceId,
    acknowledgementRevision,
  ) => {
    const initial = response();
    initial.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    api.getPermissions.mockResolvedValueOnce(initial);
    api.replaceAuthorizedUsers.mockResolvedValueOnce({
      ok: true,
      instance_id: acknowledgementInstanceId,
      authorization_revision: acknowledgementRevision,
      entries: [{
        kind: 'email',
        value: 'viewer@example.com',
        role: 'editor',
      }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', {
      name: 'permissions.actions.editAccess',
    }));
    await user.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText('permissions.roles.viewer')).toBeTruthy();
    expect(screen.queryByText('permissions.roles.editor')).toBeNull();
  });

  it('installs an offline fallback before stopping an applying-policy refresh', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    const cached = response({ source: 'cache', offline: true, cached_at: 123 });
    cached.projection.policy_sync.status = 'applying';
    cached.projection.projects[0]!.sync.status = 'pending';
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockResolvedValueOnce(cached);

    renderPage();
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('permissions.states.applyingTitle')).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(api.getPermissions).toHaveBeenCalledTimes(2);
    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();
    expect(screen.queryByText('permissions.states.applyingTitle')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.refresh' }));
      await Promise.resolve();
    });

    expect(api.getPermissions).toHaveBeenCalledTimes(3);
    expect(screen.queryByText('permissions.states.offlineTitle')).toBeNull();
  });

  it.each([
    ['access editor', 'permissions.actions.save'],
    ['Project editor', 'permissions.actions.save'],
    ['access removal', 'permissions.actions.removeAccess'],
  ] as const)('disables an already-open %s after switching offline', async (flow, actionLabel) => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    applying.projection.access.entries = [{
      kind: 'email',
      value: 'editor@example.com',
      role: 'editor',
    }];
    const cached = response({ source: 'cache', offline: true, cached_at: 123 });
    cached.projection.policy_sync.status = 'applying';
    cached.projection.projects[0]!.sync.status = 'pending';
    cached.projection.access.entries = applying.projection.access.entries;
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockResolvedValueOnce(cached);

    renderPage();
    await act(async () => { await Promise.resolve(); });

    if (flow === 'access editor') {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
    } else if (flow === 'Project editor') {
      fireEvent.click(screen.getByRole('tab', { name: 'permissions.tabs.projects' }));
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
    } else {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.removeAccess' }));
    }
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: actionLabel }).hasAttribute('disabled')).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();
    const disabledAction = within(dialog).getByRole('button', { name: actionLabel });
    expect(disabledAction.hasAttribute('disabled')).toBe(true);
    fireEvent.click(disabledAction);
    await act(async () => { await Promise.resolve(); });
    expect(api.replaceAuthorizedUsers).not.toHaveBeenCalled();
    expect(api.updateProjectAccess).not.toHaveBeenCalled();
  });

  it.each([
    ['access', 'permissions.access.narrowTitle'],
    ['Project', 'permissions.projects.narrowTitle'],
  ] as const)('disables an open %s narrowing confirmation after switching offline', async (flow, title) => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    applying.projection.access.entries = [{
      kind: 'email',
      value: 'editor@example.com',
      role: 'editor',
    }];
    const cached = response({ source: 'cache', offline: true, cached_at: 123 });
    cached.projection.policy_sync.status = 'applying';
    cached.projection.projects[0]!.sync.status = 'pending';
    cached.projection.access.entries = applying.projection.access.entries;
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockResolvedValueOnce(cached);

    renderPage();
    await act(async () => { await Promise.resolve(); });

    if (flow === 'access') {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
      fireEvent.click(screen.getByRole('radio', { name: 'permissions.roles.viewer' }));
    } else {
      fireEvent.click(screen.getByRole('tab', { name: 'permissions.tabs.projects' }));
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
      fireEvent.click(screen.getByRole('radio', { name: 'permissions.projects.modes.owner_only' }));
    }
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    const narrowingDialog = screen.getByText(title).closest('[role="dialog"]');
    expect(narrowingDialog).toBeTruthy();
    expect(within(narrowingDialog as HTMLElement).getByRole('button', {
      name: 'permissions.actions.save',
    }).hasAttribute('disabled')).toBe(false);

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();
    const disabledConfirm = within(narrowingDialog as HTMLElement).getByRole('button', {
      name: 'permissions.actions.save',
    });
    expect(disabledConfirm.hasAttribute('disabled')).toBe(true);
    fireEvent.click(disabledConfirm);
    await act(async () => { await Promise.resolve(); });
    expect(api.replaceAuthorizedUsers).not.toHaveBeenCalled();
    expect(api.updateProjectAccess).not.toHaveBeenCalled();
  });

  it('keeps a newer mutation epoch while adopting an older cache outage', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    applying.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    const staleCache = response({ source: 'cache', offline: true, cached_at: 123 });
    staleCache.projection.policy_sync.status = 'applying';
    staleCache.projection.projects[0]!.sync.status = 'pending';
    staleCache.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    api.getPermissions
      .mockResolvedValueOnce(applying)
      .mockResolvedValueOnce(staleCache);
    api.replaceAuthorizedUsers.mockResolvedValueOnce({
      ok: true,
      instance_id: 'inst-123',
      authorization_revision: 5,
      entries: [{ kind: 'email', value: 'viewer@example.com', role: 'editor' }],
    });

    renderPage();
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
    fireEvent.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    await act(async () => { await Promise.resolve(); });

    expect(screen.getByText('permissions.roles.editor')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(api.getPermissions).toHaveBeenCalledTimes(2);
    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();
    expect(screen.queryByText('permissions.states.applyingTitle')).toBeNull();
    expect(screen.getByText('permissions.roles.editor')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'permissions.actions.editAccess' })).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);
  });

  it('offers an explicit refresh after the bounded applying-policy poll', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    api.getPermissions.mockResolvedValue(applying);

    renderPage();
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    expect(api.getPermissions).toHaveBeenCalledTimes(31);
    const refresh = screen.getByRole('button', { name: 'permissions.actions.refresh' });
    const inSync = response();
    api.getPermissions.mockResolvedValueOnce(inSync);
    await act(async () => {
      refresh.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(api.getPermissions).toHaveBeenCalledTimes(32);
    expect(screen.queryByText('permissions.states.applyingTitle')).toBeNull();
  });

  it('restarts the bounded policy poll for a new applying mutation epoch', async () => {
    vi.useFakeTimers();
    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    applying.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'viewer',
    }];
    api.getPermissions.mockResolvedValue(applying);

    renderPage();
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    expect(api.getPermissions).toHaveBeenCalledTimes(31);
    expect(screen.getByRole('button', { name: 'permissions.actions.refresh' })).toBeTruthy();

    const nextApplying = response();
    nextApplying.projection.instance.authorization_revision = 5;
    nextApplying.projection.policy_sync.status = 'applying';
    nextApplying.projection.projects[0]!.sync.status = 'pending';
    nextApplying.projection.access.entries = [{
      kind: 'email',
      value: 'viewer@example.com',
      role: 'editor',
    }];
    api.getPermissions.mockResolvedValue(nextApplying);
    api.replaceAuthorizedUsers.mockResolvedValueOnce({
      ok: true,
      instance_id: 'inst-123',
      authorization_revision: 5,
      entries: nextApplying.projection.access.entries,
    });

    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.editAccess' }));
    fireEvent.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    await act(async () => { await Promise.resolve(); });

    expect(api.replaceAuthorizedUsers).toHaveBeenCalledWith(
      [{ kind: 'email', value: 'viewer@example.com', role: 'editor' }],
      4,
      'inst-123',
    );
    expect(screen.queryByRole('button', { name: 'permissions.actions.refresh' })).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(32);
    expect(screen.queryByRole('button', { name: 'permissions.actions.refresh' })).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(58_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(61);
    expect(screen.getByRole('button', { name: 'permissions.actions.refresh' })).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(61);
  });

  it.each([
    ['error', 'permissions.states.syncErrorTitle'],
    ['offline', 'permissions.states.syncOfflineTitle'],
  ] as const)('surfaces aggregate %s policy synchronization', async (status, title) => {
    const policy = response();
    policy.projection.policy_sync.status = status;
    api.getPermissions.mockResolvedValue(policy);

    renderPage();

    expect(await screen.findByText(title)).toBeTruthy();
    expect(screen.getByText(`permissions.states.sync${status === 'error' ? 'Error' : 'Offline'}Body`)).toBeTruthy();
  });

  it('does not poll cached offline policy and cleans up a live refresh timer', async () => {
    vi.useFakeTimers();
    const cached = response({ source: 'cache', offline: true, cached_at: 123 });
    cached.projection.policy_sync.status = 'applying';
    cached.projection.projects[0]!.sync.status = 'pending';
    api.getPermissions.mockResolvedValue(cached);

    const cachedPage = renderPage();
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    expect(api.getPermissions).toHaveBeenCalledOnce();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'permissions.actions.refresh' }));
      await Promise.resolve();
    });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);
    expect(screen.getByText('permissions.states.offlineTitle')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getPermissions).toHaveBeenCalledTimes(2);
    cachedPage.unmount();

    const applying = response();
    applying.projection.policy_sync.status = 'applying';
    applying.projection.projects[0]!.sync.status = 'pending';
    api.getPermissions.mockReset();
    api.getPermissions.mockResolvedValue(applying);
    const livePage = renderPage();
    await act(async () => { await Promise.resolve(); });
    livePage.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    expect(api.getPermissions).toHaveBeenCalledOnce();
  });

  it('hides deleted Project tombstones from the management surface', async () => {
    const policy = response();
    policy.projection.projects[0]!.sync.status = 'deleted';
    api.getPermissions.mockResolvedValue(policy);
    const user = userEvent.setup();

    renderPage();
    await user.click(await screen.findByRole('tab', { name: 'permissions.tabs.projects' }));

    expect(screen.getByText('permissions.projects.emptyTitle')).toBeTruthy();
    expect(screen.queryByText('Launch Plan')).toBeNull();
    expect(screen.queryByRole('button', { name: 'permissions.actions.manage' })).toBeNull();
  });

  it('keeps referenced archived groups selectable in both policy editors', async () => {
    const policy = response();
    policy.projection.directory.groups = [{
      id: 'group-archived',
      name: 'Retired Team',
      archived_at: '2026-08-17T12:00:00Z',
    }];
    policy.projection.access.entries = [{
      kind: 'organization_group',
      value: 'group-archived',
      role: 'viewer',
    }];
    policy.projection.projects[0]!.access.bindings = [{
      principal_kind: 'organization_group',
      principal_value: 'group-archived',
      access_role: 'viewer',
    }];
    api.getPermissions.mockResolvedValue(policy);
    const user = userEvent.setup();

    renderPage();
    await user.click(await screen.findByRole('button', {
      name: 'permissions.actions.editAccess',
    }));
    const accessPrincipal = screen.getByLabelText('permissions.fields.principal');
    expect((within(accessPrincipal).getByRole('option', {
      name: 'Retired Team (common.archived)',
    }) as HTMLOptionElement).selected).toBe(true);
    await user.click(screen.getByRole('button', { name: 'common.cancel' }));

    await user.click(screen.getByRole('tab', { name: 'permissions.tabs.projects' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
    const projectPrincipal = screen.getByLabelText('permissions.fields.principal');
    expect((within(projectPrincipal).getByRole('option', {
      name: 'Retired Team (common.archived)',
    }) as HTMLOptionElement).selected).toBe(true);
  });
});

describe('PermissionsPage conflict handling', () => {
  it('classifies both principal replacement and role downgrade as access narrowing', () => {
    const editor = { kind: 'email', value: 'editor@example.com', role: 'editor' } as const;

    expect(requiresAccessNarrowing(editor, {
      kind: 'email',
      value: 'replacement@example.com',
      role: 'editor',
    })).toBe(true);
    expect(requiresAccessNarrowing(editor, {
      kind: 'email',
      value: 'editor@example.com',
      role: 'viewer',
    })).toBe(true);
    expect(requiresAccessNarrowing(editor, {
      kind: 'email',
      value: 'editor@example.com',
      role: 'editor',
    })).toBe(false);
  });

  it('reconciles an access draft by principal after the authoritative list is reordered', async () => {
    const initial = response();
    initial.projection.access.entries = [
      { kind: 'email', value: 'alpha@example.com', role: 'viewer' },
      { kind: 'email', value: 'beta@example.com', role: 'viewer' },
    ];
    const latest = response();
    latest.projection.instance.authorization_revision = 5;
    latest.projection.access.entries = [
      { kind: 'email', value: 'beta@example.com', role: 'viewer' },
      { kind: 'email', value: 'alpha@example.com', role: 'viewer' },
    ];
    api.getPermissions
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(latest);
    api.replaceAuthorizedUsers
      .mockRejectedValueOnce(new PermissionsApiError(409, {
        error: 'permission_revision_conflict',
        current_revision: 5,
      }))
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 6,
        entries: [
          { kind: 'email', value: 'beta@example.com', role: 'editor' },
          { kind: 'email', value: 'alpha@example.com', role: 'viewer' },
        ],
      });
    const user = userEvent.setup();
    renderPage();

    const editButtons = await screen.findAllByRole('button', { name: 'permissions.actions.editAccess' });
    await user.click(editButtons[1]!);
    await user.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    expect(await screen.findByText('permissions.states.conflictTitle')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'permissions.actions.retrySave' }));

    await waitFor(() => expect(api.replaceAuthorizedUsers).toHaveBeenCalledTimes(2));
    expect(api.replaceAuthorizedUsers.mock.calls[1]).toEqual([
      [
        { kind: 'email', value: 'beta@example.com', role: 'editor' },
        { kind: 'email', value: 'alpha@example.com', role: 'viewer' },
      ],
      5,
      'inst-123',
    ]);
  });

  it('closes an access addition when conflict refresh shows the draft was already applied', async () => {
    const initial = response();
    const latest = response();
    latest.projection.instance.authorization_revision = 5;
    latest.projection.access.entries = [
      { kind: 'email', value: 'added@example.com', role: 'viewer' },
    ];
    api.getPermissions
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(latest);
    api.replaceAuthorizedUsers.mockRejectedValueOnce(new PermissionsApiError(409, {
      error: 'permission_revision_conflict',
      current_revision: 5,
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'permissions.actions.addAccess' }));
    await user.type(screen.getByLabelText('permissions.fields.principal'), 'added@example.com');
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText('added@example.com')).toBeTruthy();
    expect(api.replaceAuthorizedUsers).toHaveBeenCalledOnce();
  });

  it.each([
    { draftRole: 'editor' as const, concurrentRole: 'viewer' as const, narrows: false },
    { draftRole: 'viewer' as const, concurrentRole: 'editor' as const, narrows: true },
  ])('rebases an access addition onto a concurrently created matching principal', async ({
    draftRole,
    concurrentRole,
    narrows,
  }) => {
    const initial = response();
    const latest = response();
    latest.projection.instance.authorization_revision = 5;
    latest.projection.access.entries = [
      { kind: 'email', value: 'concurrent@example.com', role: concurrentRole },
    ];
    api.getPermissions
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(latest);
    api.replaceAuthorizedUsers
      .mockRejectedValueOnce(new PermissionsApiError(409, {
        error: 'permission_revision_conflict',
        current_revision: 5,
      }))
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 6,
        entries: [{ kind: 'email', value: 'concurrent@example.com', role: draftRole }],
      });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'permissions.actions.addAccess' }));
    await user.type(screen.getByLabelText('permissions.fields.principal'), 'concurrent@example.com');
    if (draftRole === 'editor') {
      await user.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    }
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    expect(await screen.findByText('permissions.states.conflictTitle')).toBeTruthy();
    expect(screen.getByText('permissions.access.editTitle')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'permissions.actions.retrySave' }));
    if (narrows) {
      const narrowingDialog = (await screen.findByText('permissions.access.narrowTitle')).closest('[role="dialog"]');
      await user.click(within(narrowingDialog as HTMLElement).getByRole('button', {
        name: 'permissions.actions.save',
      }));
    }

    await waitFor(() => expect(api.replaceAuthorizedUsers).toHaveBeenCalledTimes(2));
    expect(api.replaceAuthorizedUsers.mock.calls[1]).toEqual([
      [{ kind: 'email', value: 'concurrent@example.com', role: draftRole }],
      5,
      'inst-123',
    ]);
    expect(screen.queryByText('permissions.errors.duplicate_access_principal')).toBeNull();
  });

  it('closes a principal replacement when conflict refresh shows it was already applied', async () => {
    const initial = response();
    initial.projection.access.entries = [
      { kind: 'email', value: 'old@example.com', role: 'viewer' },
    ];
    const latest = response();
    latest.projection.instance.authorization_revision = 5;
    latest.projection.access.entries = [
      { kind: 'email', value: 'replacement@example.com', role: 'viewer' },
    ];
    api.getPermissions
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(latest);
    api.replaceAuthorizedUsers.mockRejectedValueOnce(new PermissionsApiError(409, {
      error: 'permission_revision_conflict',
      current_revision: 5,
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'permissions.actions.editAccess' }));
    await user.clear(screen.getByLabelText('permissions.fields.principal'));
    await user.type(screen.getByLabelText('permissions.fields.principal'), 'replacement@example.com');
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    const narrowingDialog = (await screen.findByText('permissions.access.narrowTitle')).closest('[role="dialog"]');
    await user.click(within(narrowingDialog as HTMLElement).getByRole('button', {
      name: 'permissions.actions.save',
    }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText('replacement@example.com')).toBeTruthy();
    expect(screen.queryByText('old@example.com')).toBeNull();
    expect(api.replaceAuthorizedUsers).toHaveBeenCalledOnce();
  });

  it('keeps an access draft mounted when the conflict refresh fails', async () => {
    const initial = response();
    initial.projection.access.entries = [
      { kind: 'email', value: 'viewer@example.com', role: 'viewer' },
    ];
    api.getPermissions
      .mockResolvedValueOnce(initial)
      .mockRejectedValueOnce(new PermissionsApiError(503, {
        error: 'cloud_policy_unavailable',
      }));
    api.replaceAuthorizedUsers.mockRejectedValueOnce(new PermissionsApiError(409, {
      error: 'permission_revision_conflict',
      current_revision: 5,
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'permissions.actions.editAccess' }));
    await user.click(screen.getByRole('radio', { name: 'permissions.roles.editor' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    expect(await screen.findByText('permissions.states.conflictRefreshBody')).toBeTruthy();
    expect(screen.getByText('permissions.errors.permissions_refresh_failed')).toBeTruthy();
    expect(screen.getByRole('radio', {
      name: 'permissions.roles.editor',
    }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText('owner@example.com')).toBeTruthy();
    expect(api.replaceAuthorizedUsers).toHaveBeenCalledOnce();
  });

  it('confirms an access-role reduction before committing it', async () => {
    const initial = response();
    initial.projection.access.entries = [
      { kind: 'email', value: 'editor@example.com', role: 'editor' },
    ];
    api.getPermissions.mockResolvedValueOnce(initial);
    api.replaceAuthorizedUsers.mockResolvedValueOnce({
      ok: true,
      instance_id: 'inst-123',
      authorization_revision: 5,
      entries: [{ kind: 'email', value: 'editor@example.com', role: 'viewer' }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: 'permissions.actions.editAccess' }));
    await user.click(screen.getByRole('radio', { name: 'permissions.roles.viewer' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    const narrowingTitle = await screen.findByText('permissions.access.narrowTitle');
    const narrowingDialog = narrowingTitle.closest('[role="dialog"]');
    expect(narrowingDialog).toBeTruthy();
    expect(api.replaceAuthorizedUsers).not.toHaveBeenCalled();
    await user.click(within(narrowingDialog as HTMLElement).getByRole('button', {
      name: 'permissions.actions.save',
    }));

    await waitFor(() => expect(api.replaceAuthorizedUsers).toHaveBeenCalledOnce());
    expect(api.replaceAuthorizedUsers).toHaveBeenCalledWith(
      [{ kind: 'email', value: 'editor@example.com', role: 'viewer' }],
      4,
      'inst-123',
    );
  });

  it('re-resolves an access removal by principal after a conflict reorders the list', async () => {
    const initial = response();
    initial.projection.access.entries = [
      { kind: 'email', value: 'alpha@example.com', role: 'viewer' },
      { kind: 'email', value: 'beta@example.com', role: 'viewer' },
    ];
    const latest = response();
    latest.projection.instance.authorization_revision = 5;
    latest.projection.access.entries = [
      { kind: 'email', value: 'beta@example.com', role: 'viewer' },
      { kind: 'email', value: 'alpha@example.com', role: 'viewer' },
    ];
    api.getPermissions
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(latest);
    api.replaceAuthorizedUsers
      .mockRejectedValueOnce(new PermissionsApiError(409, {
        error: 'permission_revision_conflict',
        current_revision: 5,
      }))
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 6,
        entries: [{ kind: 'email', value: 'beta@example.com', role: 'viewer' }],
      });
    const user = userEvent.setup();
    renderPage();

    const removeButtons = await screen.findAllByRole('button', {
      name: 'permissions.actions.removeAccess',
    });
    await user.click(removeButtons[0]!);
    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', {
      name: 'permissions.actions.removeAccess',
    });
    await user.click(confirm);

    await waitFor(() => expect(api.getPermissions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(confirm.hasAttribute('disabled')).toBe(false));
    await user.click(confirm);

    await waitFor(() => expect(api.replaceAuthorizedUsers).toHaveBeenCalledTimes(2));
    expect(api.replaceAuthorizedUsers.mock.calls[1]).toEqual([
      [{ kind: 'email', value: 'beta@example.com', role: 'viewer' }],
      5,
      'inst-123',
    ]);
  });

  it('surfaces a non-conflict access deletion error inside the confirmation flow', async () => {
    const initial = response();
    initial.projection.access.entries = [
      { kind: 'email', value: 'viewer@example.com', role: 'viewer' },
    ];
    api.getPermissions.mockResolvedValueOnce(initial);
    api.replaceAuthorizedUsers.mockRejectedValueOnce(new PermissionsApiError(403, {
      error: 'permission_authority_cloud',
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', {
      name: 'permissions.actions.removeAccess',
    }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', {
      name: 'permissions.actions.removeAccess',
    }));

    expect(await within(dialog).findByText(
      'permissions.errors.permission_authority_cloud',
    )).toBeTruthy();
    expect(within(dialog).getByText('permissions.states.errorTitle')).toBeTruthy();
    expect(api.replaceAuthorizedUsers).toHaveBeenCalledOnce();
  });

  it('encodes Owner only as restricted with an empty binding set', async () => {
    api.updateProjectAccess.mockResolvedValue({
      ok: true,
      instance_id: 'inst-123',
      authorization_revision: 5,
      project: {
        ...response().projection.projects[0]!,
        access: { mode: 'restricted', revision: 2, bindings: [] },
      },
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('tab', { name: 'permissions.tabs.projects' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
    await user.click(screen.getByRole('radio', { name: 'permissions.projects.modes.owner_only' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    await waitFor(() => expect(api.updateProjectAccess).toHaveBeenCalledOnce());
    expect(api.updateProjectAccess).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: 'project-1' }),
      'restricted',
      [],
      1,
      'inst-123',
    );
    expect(await screen.findByText('permissions.projects.modes.owner_only')).toBeTruthy();
  });

  it('re-runs Project narrowing preflight against the refreshed baseline', async () => {
    const latest = response();
    latest.projection.projects[0]!.access.revision = 2;
    latest.projection.projects[0]!.access.mode = 'inherit';
    latest.projection.projects[0]!.access.bindings = [];
    api.getPermissions
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(latest);
    api.updateProjectAccess
      .mockRejectedValueOnce(new PermissionsApiError(409, {
        error: 'permission_revision_conflict',
        current_revision: 2,
      }))
      .mockResolvedValueOnce({
        ok: true,
        instance_id: 'inst-123',
        authorization_revision: 5,
        project: {
          ...latest.projection.projects[0]!,
          access: {
            ...latest.projection.projects[0]!.access,
            bindings: [{
              principal_kind: 'email',
              principal_value: 'viewer@example.com',
              access_role: 'editor',
            }],
          },
        },
      });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('tab', { name: 'permissions.tabs.projects' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
    await user.selectOptions(screen.getByLabelText('permissions.fields.role'), 'editor');
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    expect(await screen.findByText('permissions.states.conflictTitle')).toBeTruthy();
    expect((screen.getByLabelText('permissions.fields.role') as HTMLSelectElement).value).toBe('editor');
    const retry = screen.getByRole('button', { name: 'permissions.actions.retrySave' });
    await user.click(retry);

    const narrowingTitle = await screen.findByText('permissions.projects.narrowTitle');
    const narrowingDialog = narrowingTitle.closest('[role="dialog"]');
    expect(narrowingDialog).toBeTruthy();
    expect(api.updateProjectAccess).toHaveBeenCalledOnce();
    await user.click(within(narrowingDialog as HTMLElement).getByRole('button', {
      name: 'permissions.actions.save',
    }));

    await waitFor(() => expect(api.updateProjectAccess).toHaveBeenCalledTimes(2));
    expect(api.updateProjectAccess.mock.calls[1]).toEqual([
      expect.objectContaining({ project_id: 'project-1' }),
      'restricted',
      [{
        principal_kind: 'email',
        principal_value: 'viewer@example.com',
        access_role: 'editor',
      }],
      2,
      'inst-123',
    ]);
  });

  it('keeps a Project draft mounted when the conflict refresh fails', async () => {
    api.getPermissions
      .mockResolvedValueOnce(response())
      .mockRejectedValueOnce(new PermissionsApiError(503, {
        error: 'cloud_policy_unavailable',
      }));
    api.updateProjectAccess.mockRejectedValueOnce(new PermissionsApiError(409, {
      error: 'permission_revision_conflict',
      current_revision: 2,
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('tab', { name: 'permissions.tabs.projects' }));
    await user.click(screen.getByRole('button', { name: 'permissions.actions.manage' }));
    await user.selectOptions(screen.getByLabelText('permissions.fields.role'), 'editor');
    await user.click(screen.getByRole('button', { name: 'permissions.actions.save' }));

    expect(await screen.findByText('permissions.states.conflictRefreshBody')).toBeTruthy();
    expect(screen.getByText('permissions.errors.permissions_refresh_failed')).toBeTruthy();
    expect((screen.getByLabelText('permissions.fields.role') as HTMLSelectElement).value).toBe(
      'editor',
    );
    expect(screen.getByText('Launch Plan')).toBeTruthy();
    expect(api.updateProjectAccess).toHaveBeenCalledOnce();
  });
});
