/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { PermissionsApiError } from './api';
import { PermissionsPage } from './PermissionsPage';
import type { PermissionsResponse } from './types';

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
  render(
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

beforeEach(() => {
  api.getPermissions.mockResolvedValue(response());
  api.replaceAuthorizedUsers.mockReset();
  api.updateProjectAccess.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('PermissionsPage state model', () => {
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

  it('keeps cached offline policy distinct from a live empty policy', async () => {
    api.getPermissions.mockResolvedValue(response({ source: 'cache', offline: true, cached_at: 123 }));

    renderPage();

    expect(await screen.findByText('permissions.states.offlineTitle')).toBeTruthy();
    expect(screen.getByText('permissions.access.emptyTitle')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /permissions.actions.addAccess/ })).toBeNull();
  });

  it('lets a Viewer read policy while keeping mutation controls absent', async () => {
    renderPage(false);

    expect(await screen.findByText('permissions.states.readOnlyTitle')).toBeTruthy();
    expect(screen.getByText('owner@example.com')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /permissions.actions.addAccess/ })).toBeNull();
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
});

describe('PermissionsPage conflict handling', () => {
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
    ]);
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
    ]);
  });

  it('sends owner-only as the exact Backend access mode', async () => {
    api.updateProjectAccess.mockResolvedValue({
      ok: true,
      authorization_revision: 5,
      project: {
        ...response().projection.projects[0]!,
        access: { mode: 'owner_only', revision: 2, bindings: [] },
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
      'owner_only',
      [],
      1,
    );
  });

  it('keeps the Project draft, refreshes the revision, and requires explicit retry', async () => {
    const latest = response();
    latest.projection.projects[0]!.access.revision = 2;
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
    ]);
  });
});
