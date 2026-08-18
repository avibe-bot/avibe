/** @vitest-environment jsdom */

import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PermissionsApiError } from '@/features/permissions/api';
import { requiresResourcePolicyNarrowing } from '@/features/permissions/policy';
import type {
  DirectoryGroup,
  PermissionResource,
  PermissionsResponse,
  ResourceAccessResponse,
} from '@/features/permissions/types';
import type { ShowPageAccess } from '@/lib/showPageAccess';

import { ShowPageWorkspaceAccessControl } from './ShowPageWorkspaceAccessControl';

const api = vi.hoisted(() => ({
  getPermissions: vi.fn(),
  getResourceAccess: vi.fn(),
  updateResourceAccess: vi.fn(),
}));

vi.mock('@/features/permissions/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/permissions/api')>()),
  getPermissions: api.getPermissions,
  getResourceAccess: api.getResourceAccess,
  updateResourceAccess: api.updateResourceAccess,
}));

const translations: Record<string, string> = {
  'chat.showPage.workspaceAccess': 'Workspace access',
  'chat.showPage.workspaceAccessDesc': 'Controls authenticated access inside this Avibe. Link access remains separate.',
  'chat.showPage.loadingWorkspaceAccess': 'Loading Workspace access...',
  'chat.showPage.workspacePersonal': 'This Show Page belongs to a Personal Avibe.',
  'chat.showPage.workspaceUnmanaged': 'Workspace access is unavailable until this Avibe is paired.',
  'chat.showPage.workspacePending': 'Organization ownership is known, but its exact binding is temporarily unavailable. Access remains private.',
  'chat.showPage.workspaceOwnershipConflict': 'This Show Page is bound to a different ownership domain. Access remains private until the conflict is resolved.',
  'chat.showPage.workspaceModes.private': 'Private',
  'chat.showPage.workspaceModes.organization': 'Organization',
  'chat.showPage.workspaceModes.scope': 'Selected groups',
  'chat.showPage.workspaceHelp.private': 'Only the page owner and Instance Owner can use it.',
  'chat.showPage.workspaceHelp.public': 'Authenticated members of this Organization can use it.',
  'chat.showPage.workspaceHelp.scope': 'Only members of the selected Organization groups can use it.',
  'chat.showPage.workspaceGroups': 'Organization groups',
  'chat.showPage.workspaceArchived': 'Archived',
  'chat.showPage.workspaceNoGroups': 'No active groups are available.',
  'chat.showPage.workspaceGroupRequired': 'Select at least one group.',
  'chat.showPage.workspaceRevisionConflict': 'Workspace access changed elsewhere. Your draft was kept on the latest revision.',
  'chat.showPage.workspaceOffline': 'The last known policy is shown while Permissions is offline. Editing is disabled.',
  'chat.showPage.workspaceReadOnly': 'Avibe Cloud owns this policy. Edit it in Avibe Cloud.',
  'chat.showPage.workspaceOwnerOnly': 'Only the Instance Owner can change Workspace access.',
  'chat.showPage.workspaceLoadError': 'Workspace access could not be loaded.',
  'chat.showPage.workspaceNarrowTitle': 'Narrow Workspace access?',
  'chat.showPage.workspaceNarrowBody': 'Some people may lose access as soon as this policy is applied.',
  'chat.showPage.applyWorkspaceAccess': 'Apply',
  'chat.showPage.workspaceSync.pending': 'The policy is waiting for this Avibe to apply it.',
  'chat.showPage.workspaceSync.offline': 'This Avibe has not acknowledged the latest policy.',
  'chat.showPage.workspaceSync.error': 'The latest policy could not be applied.',
  'common.retry': 'Retry',
};

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) => (
      translations[key] ?? options?.defaultValue ?? key
    ),
  }),
}));

const activeGroup: DirectoryGroup = {
  id: 'group-active',
  name: 'Design',
  archived_at: null,
};

const archivedGroup: DirectoryGroup = {
  id: 'group-archived',
  name: 'Retired Team',
  archived_at: '2026-08-18T00:00:00Z',
};

const newArchivedGroup: DirectoryGroup = {
  id: 'group-new-archived',
  name: 'Former Operations',
  archived_at: '2026-08-18T01:00:00Z',
};

const showPageAccess = (overrides: Partial<ShowPageAccess> = {}): ShowPageAccess => ({
  ok: true,
  mode: 'organization',
  ownership_status: 'unchanged',
  instance_id: 'inst-1',
  organization_id: 'org-1',
  policy_organization_id: 'org-1',
  access_level: 'private',
  group_ids: [],
  policy_revision: 4,
  last_applied_control_plane_revision: 4,
  can_use: true,
  can_manage: true,
  can_publish_public: true,
  ...overrides,
});

const permissions = ({
  groups = [activeGroup],
  offline = false,
  permissionAuthority = 'instance',
  localMutationAllowed = true,
}: {
  groups?: DirectoryGroup[];
  offline?: boolean;
  permissionAuthority?: 'instance' | 'cloud';
  localMutationAllowed?: boolean;
} = {}): PermissionsResponse => ({
  ok: true,
  source: offline ? 'cache' : 'live',
  offline,
  cached_at: offline ? 1 : null,
  projection: {
    schema_version: 1,
    instance: {
      id: 'inst-1',
      organization: { id: 'org-1', name: 'Example Organization' },
      access_mode: 'allowlist',
      permission_authority: permissionAuthority,
      local_mutation_allowed: localMutationAllowed,
      authorization_revision: 7,
    },
    capabilities: ['instance.permissions.read', 'instance.permissions.mutate'],
    access: { owner: { email: 'owner@example.com', role: 'owner' }, entries: [] },
    directory: { members: [], groups },
    projects: [],
    policy_sync: {
      status: offline ? 'offline' : 'in_sync',
      projects: { active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 },
      resources: { active: 1, error: 0, offline: 0, applying: 0, in_sync: 1 },
    },
  },
});

const resource = ({
  sessionId = 'ses-1',
  level = 'private',
  groupIds = [],
  revision = 4,
  status = 'in_sync',
}: {
  sessionId?: string;
  level?: PermissionResource['access']['access_level'];
  groupIds?: string[];
  revision?: number;
  status?: PermissionResource['sync']['status'];
} = {}): PermissionResource => ({
  instance_id: 'inst-1',
  resource_kind: 'show_page',
  resource_id: sessionId,
  display_name: sessionId,
  owner_user_id: 'owner-1',
  access: { access_level: level, group_ids: groupIds, revision },
  sync: {
    status,
    desired_acl_revision: revision,
    applied_acl_revision: status === 'in_sync' ? revision : Math.max(0, revision - 1),
    last_synced_at: null,
  },
});

const renderControl = (
  access: ShowPageAccess = showPageAccess(),
  options: { sessionId?: string; canManageInstance?: boolean } = {},
) => render(
  <ShowPageWorkspaceAccessControl
    access={access}
    active
    canManageInstance={options.canManageInstance ?? true}
    sessionId={options.sessionId ?? 'ses-1'}
  />,
);

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

beforeEach(() => {
  api.getPermissions.mockReset();
  api.getPermissions.mockResolvedValue(permissions());
  api.getResourceAccess.mockReset();
  api.getResourceAccess.mockResolvedValue({ resource: resource() });
  api.updateResourceAccess.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ShowPageWorkspaceAccessControl', () => {
  it('classifies every Workspace policy reduction at the shared boundary', () => {
    expect(requiresResourcePolicyNarrowing('public', [], 'private', [])).toBe(true);
    expect(requiresResourcePolicyNarrowing('public', [], 'scope', ['group-active'])).toBe(true);
    expect(requiresResourcePolicyNarrowing('scope', ['group-active'], 'private', [])).toBe(true);
    expect(requiresResourcePolicyNarrowing(
      'scope',
      ['group-active', 'group-archived'],
      'scope',
      ['group-active'],
    )).toBe(true);
    expect(requiresResourcePolicyNarrowing('scope', ['group-active'], 'public', [])).toBe(false);
    expect(requiresResourcePolicyNarrowing('private', [], 'public', [])).toBe(false);
    expect(requiresResourcePolicyNarrowing('private', [], 'scope', ['group-active'])).toBe(false);
  });

  it('confirms a Workspace policy reduction before sending it', async () => {
    api.getResourceAccess.mockResolvedValue({
      resource: resource({ level: 'public' }),
    });
    api.updateResourceAccess.mockResolvedValue({
      ok: true,
      resource: resource({ level: 'private', revision: 5 }),
    });
    const user = userEvent.setup();
    renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Private' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    const dialog = screen.getByText('Narrow Workspace access?').closest('[role="dialog"]');
    expect(dialog).toBeTruthy();
    expect(api.updateResourceAccess).not.toHaveBeenCalled();

    await user.click(within(dialog as HTMLElement).getByRole('button', { name: 'Apply' }));
    expect(api.updateResourceAccess).toHaveBeenCalledWith(
      { resource_kind: 'show_page', resource_id: 'ses-1' },
      'private',
      [],
      4,
      'inst-1',
    );
  });

  it('saves Organization and selected-group policies and reloads the canonical group selection', async () => {
    api.updateResourceAccess
      .mockResolvedValueOnce({ ok: true, resource: resource({ level: 'public', revision: 5 }) })
      .mockResolvedValueOnce({
        ok: true,
        resource: resource({ level: 'scope', groupIds: ['group-active'], revision: 6 }),
      });
    const user = userEvent.setup();
    const view = renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Organization' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(api.updateResourceAccess).toHaveBeenLastCalledWith(
      { resource_kind: 'show_page', resource_id: 'ses-1' },
      'public',
      [],
      4,
      'inst-1',
    );

    await user.click(screen.getByRole('radio', { name: 'Selected groups' }));
    await user.click(screen.getByRole('checkbox', { name: 'Design' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    const narrowingDialog = screen.getByText('Narrow Workspace access?').closest('[role="dialog"]');
    await user.click(within(narrowingDialog as HTMLElement).getByRole('button', { name: 'Apply' }));
    expect(api.updateResourceAccess).toHaveBeenLastCalledWith(
      { resource_kind: 'show_page', resource_id: 'ses-1' },
      'scope',
      ['group-active'],
      5,
      'inst-1',
    );

    view.unmount();
    api.getResourceAccess.mockResolvedValue({
      resource: resource({ level: 'scope', groupIds: ['group-active'], revision: 6 }),
    });
    renderControl();

    expect((await screen.findByRole('radio', { name: 'Selected groups' })).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('checkbox', { name: 'Design' }).getAttribute('aria-checked')).toBe('true');
  });

  it('applies private-to-scope audience expansion without a narrowing confirmation', async () => {
    api.updateResourceAccess.mockResolvedValue({
      ok: true,
      resource: resource({ level: 'scope', groupIds: ['group-active'], revision: 5 }),
    });
    const user = userEvent.setup();
    renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Selected groups' }));
    await user.click(screen.getByRole('checkbox', { name: 'Design' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(screen.queryByText('Narrow Workspace access?')).toBeNull();
    expect(api.updateResourceAccess).toHaveBeenCalledWith(
      { resource_kind: 'show_page', resource_id: 'ses-1' },
      'scope',
      ['group-active'],
      4,
      'inst-1',
    );
  });

  it('keeps bound archived groups visible and selected without offering unbound archived groups', async () => {
    api.getPermissions.mockResolvedValue(permissions({
      groups: [activeGroup, archivedGroup, newArchivedGroup],
    }));
    api.getResourceAccess.mockResolvedValue({
      resource: resource({ level: 'scope', groupIds: ['group-archived'] }),
    });
    api.updateResourceAccess.mockResolvedValue({
      ok: true,
      resource: resource({
        level: 'scope',
        groupIds: ['group-active', 'group-archived'],
        revision: 5,
      }),
    });
    const user = userEvent.setup();
    renderControl();

    const archived = await screen.findByRole('checkbox', { name: 'Retired Team' });
    expect(archived.getAttribute('aria-checked')).toBe('true');
    expect((archived as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Archived')).toBeTruthy();
    expect(screen.queryByText('Former Operations')).toBeNull();

    await user.click(screen.getByRole('radio', { name: 'Organization' }));
    await user.click(screen.getByRole('radio', { name: 'Selected groups' }));
    expect(screen.getByRole('checkbox', { name: 'Retired Team' }).getAttribute('aria-checked')).toBe('true');

    await user.click(screen.getByRole('checkbox', { name: 'Design' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(api.updateResourceAccess).toHaveBeenCalledWith(
      { resource_kind: 'show_page', resource_id: 'ses-1' },
      'scope',
      ['group-active', 'group-archived'],
      4,
      'inst-1',
    );
  });

  it('preserves the draft across a 409 and folds newly bound archived groups into the retry', async () => {
    api.getPermissions
      .mockResolvedValueOnce(permissions())
      .mockResolvedValueOnce(permissions({ groups: [activeGroup, archivedGroup] }));
    api.getResourceAccess
      .mockResolvedValueOnce({ resource: resource() })
      .mockResolvedValueOnce({
        resource: resource({ level: 'scope', groupIds: ['group-archived'], revision: 5 }),
      });
    api.updateResourceAccess
      .mockRejectedValueOnce(new PermissionsApiError(409, {
        error: 'permission_revision_conflict',
        current_revision: 5,
      }))
      .mockResolvedValueOnce({
        ok: true,
        resource: resource({
          level: 'scope',
          groupIds: ['group-active', 'group-archived'],
          revision: 6,
        }),
      });
    const user = userEvent.setup();
    renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Selected groups' }));
    await user.click(screen.getByRole('checkbox', { name: 'Design' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByText(/Your draft was kept/)).toBeTruthy();
    expect(screen.getByRole('checkbox', { name: 'Design' }).getAttribute('aria-checked')).toBe('true');
    const archived = screen.getByRole('checkbox', { name: 'Retired Team' });
    expect(archived.getAttribute('aria-checked')).toBe('true');
    expect((archived as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(api.updateResourceAccess).toHaveBeenLastCalledWith(
      { resource_kind: 'show_page', resource_id: 'ses-1' },
      'scope',
      ['group-active', 'group-archived'],
      5,
      'inst-1',
    );
  });

  it('disables the mounted editor when the authoritative conflict refresh fails', async () => {
    api.getPermissions
      .mockResolvedValueOnce(permissions())
      .mockRejectedValueOnce(new PermissionsApiError(503, {
        error: 'permissions_backend_unavailable',
      }));
    api.updateResourceAccess.mockRejectedValueOnce(new PermissionsApiError(409, {
      error: 'permission_revision_conflict',
      current_revision: 5,
    }));
    const user = userEvent.setup();
    renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Organization' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    expect(await screen.findByText('Workspace access could not be loaded.')).toBeTruthy();
    expect((screen.getByRole('radio', { name: 'Private' }) as HTMLButtonElement).disabled).toBe(true);
    const apply = screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    await user.click(apply);
    expect(api.updateResourceAccess).toHaveBeenCalledTimes(1);
  });

  it('preserves the draft when a failed PUT is recovered with Retry', async () => {
    api.getPermissions.mockResolvedValueOnce(permissions()).mockResolvedValueOnce(permissions());
    api.getResourceAccess
      .mockResolvedValueOnce({ resource: resource() })
      .mockResolvedValueOnce({ resource: resource() });
    api.updateResourceAccess.mockRejectedValueOnce(new PermissionsApiError(503, {
      error: 'permissions_backend_unavailable',
    }));
    const user = userEvent.setup();
    renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Selected groups' }));
    await user.click(screen.getByRole('checkbox', { name: 'Design' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(await screen.findByText('Workspace access could not be loaded.')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('checkbox', { name: 'Design' });
    expect(screen.getByRole('radio', { name: 'Selected groups' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('checkbox', { name: 'Design' }).getAttribute('aria-checked')).toBe('true');
    expect(api.updateResourceAccess).toHaveBeenCalledOnce();
  });

  it('preserves the draft when the conflict refresh fails before a Retry', async () => {
    api.getPermissions
      .mockResolvedValueOnce(permissions())
      .mockRejectedValueOnce(new PermissionsApiError(503, {
        error: 'permissions_backend_unavailable',
      }))
      .mockResolvedValueOnce(permissions());
    api.getResourceAccess
      .mockResolvedValueOnce({ resource: resource() })
      .mockResolvedValueOnce({ resource: resource() })
      .mockResolvedValueOnce({ resource: resource() });
    api.updateResourceAccess.mockRejectedValueOnce(new PermissionsApiError(409, {
      error: 'permission_revision_conflict',
      current_revision: 5,
    }));
    const user = userEvent.setup();
    renderControl();

    await user.click(await screen.findByRole('radio', { name: 'Selected groups' }));
    await user.click(screen.getByRole('checkbox', { name: 'Design' }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    expect(await screen.findByText('Workspace access could not be loaded.')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('checkbox', { name: 'Design' });
    expect(screen.getByRole('radio', { name: 'Selected groups' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('checkbox', { name: 'Design' }).getAttribute('aria-checked')).toBe('true');
    expect(api.updateResourceAccess).toHaveBeenCalledOnce();
  });

  it('fails closed for personal, pending, and conflicting ownership without contacting Permissions', () => {
    const personal = renderControl(showPageAccess({
      mode: 'personal',
      ownership_status: 'unchanged',
      organization_id: null,
      policy_organization_id: null,
    }));
    expect(screen.getByText(/belongs to a Personal Avibe/)).toBeTruthy();
    personal.unmount();

    const pending = renderControl(showPageAccess({
      mode: 'organization_pending',
      ownership_status: 'pending',
      organization_id: null,
      policy_organization_id: null,
    }));
    expect(screen.getByText(/temporarily unavailable/)).toBeTruthy();
    pending.unmount();

    renderControl(showPageAccess({
      ownership_status: 'conflict',
      policy_organization_id: 'org-other',
    }));
    expect(screen.getByText(/different ownership domain/)).toBeTruthy();
    expect(api.getPermissions).not.toHaveBeenCalled();
    expect(api.getResourceAccess).not.toHaveBeenCalled();
  });

  it('renders offline, Cloud-owned, and non-owner policy as read-only', async () => {
    api.getPermissions.mockResolvedValue(permissions({ offline: true }));
    const offline = renderControl();
    expect(await screen.findByText(/last known policy/)).toBeTruthy();
    expect((screen.getByRole('radio', { name: 'Private' }) as HTMLButtonElement).disabled).toBe(true);
    offline.unmount();

    api.getPermissions.mockResolvedValue(permissions({
      permissionAuthority: 'cloud',
      localMutationAllowed: false,
    }));
    const cloud = renderControl();
    expect(await screen.findByText(/Avibe Cloud owns this policy/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(true);
    cloud.unmount();

    api.getPermissions.mockResolvedValue(permissions());
    renderControl(showPageAccess(), { canManageInstance: false });
    expect(await screen.findByText(/Only the Instance Owner/)).toBeTruthy();
    expect((screen.getByRole('radio', { name: 'Private' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('ignores an older session response after the control switches pages', async () => {
    const oldPermissions = deferred<PermissionsResponse>();
    const oldResource = deferred<ResourceAccessResponse>();
    const oldGroup = { id: 'group-old', name: 'Old Page Group', archived_at: null };
    const nextGroup = { id: 'group-next', name: 'Next Page Group', archived_at: null };
    api.getPermissions
      .mockImplementationOnce(() => oldPermissions.promise)
      .mockResolvedValueOnce(permissions({ groups: [nextGroup] }));
    api.getResourceAccess.mockImplementation((identity: { resource_id: string }) => (
      identity.resource_id === 'ses-old'
        ? oldResource.promise
        : Promise.resolve({
          resource: resource({
            sessionId: 'ses-next',
            level: 'scope',
            groupIds: ['group-next'],
          }),
        })
    ));
    const view = renderControl(showPageAccess(), { sessionId: 'ses-old' });

    view.rerender(
      <ShowPageWorkspaceAccessControl
        access={showPageAccess()}
        active
        canManageInstance
        sessionId="ses-next"
      />,
    );
    expect(await screen.findByRole('checkbox', { name: 'Next Page Group' })).toBeTruthy();

    await act(async () => {
      oldPermissions.resolve(permissions({ groups: [oldGroup] }));
      oldResource.resolve({
        resource: resource({
          sessionId: 'ses-old',
          level: 'scope',
          groupIds: ['group-old'],
        }),
      });
      await Promise.resolve();
    });

    expect(screen.getByRole('checkbox', { name: 'Next Page Group' })).toBeTruthy();
    expect(screen.queryByText('Old Page Group')).toBeNull();
  });
});
