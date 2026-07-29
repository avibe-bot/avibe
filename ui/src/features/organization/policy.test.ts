import { describe, expect, it } from 'vitest';

import {
  aggregateSyncStatus,
  hasDuplicateProjectPrincipals,
  isCurrentOrganizationLoad,
  normalizeOrganizationPrincipal,
  organizationSwitchDestination,
  requiresMemberRoleDowngradeConfirmation,
  requiresProjectAccessNarrowingConfirmation,
  requiresResourceAccessNarrowingConfirmation,
} from './policy';

describe('Organization policy helpers', () => {
  it('normalizes email and domain principals like the runtime validator', () => {
    expect(normalizeOrganizationPrincipal('email', ' Viewer@Example.com ')).toBe('viewer@example.com');
    expect(normalizeOrganizationPrincipal('email_domain', ' @Example.com ')).toBe('example.com');
    expect(normalizeOrganizationPrincipal('organization_group', ' group-1 ')).toBe('group-1');
  });

  it('rejects duplicate normalized Project principals without collapsing roles', () => {
    expect(hasDuplicateProjectPrincipals([
      { principal_kind: 'email', principal_value: 'Member@Example.com', access_role: 'viewer' },
      { principal_kind: 'email', principal_value: ' member@example.com ', access_role: 'editor' },
    ])).toBe(true);
    expect(hasDuplicateProjectPrincipals([
      { principal_kind: 'email', principal_value: 'member@example.com', access_role: 'viewer' },
      { principal_kind: 'email_domain', principal_value: 'member@example.com', access_role: 'viewer' },
    ])).toBe(false);
  });

  it('derives child sync status in the documented severity order', () => {
    expect(aggregateSyncStatus({ active: 4, error: 1, offline: 1, applying: 1, in_sync: 1 })).toBe('error');
    expect(aggregateSyncStatus({ active: 3, error: 0, offline: 1, applying: 1, in_sync: 1 })).toBe('offline');
    expect(aggregateSyncStatus({ active: 2, error: 0, offline: 0, applying: 1, in_sync: 1 })).toBe('applying');
    expect(aggregateSyncStatus({ active: 1, error: 0, offline: 0, applying: 0, in_sync: 1 })).toBe('in_sync');
    expect(aggregateSyncStatus({ active: 0, error: 0, offline: 0, applying: 0, in_sync: 0 })).toBe('none');
  });

  it('accepts only the latest load for the currently selected Organization', () => {
    expect(isCurrentOrganizationLoad('org-b', 'org-b', 4, 4)).toBe(true);
    expect(isCurrentOrganizationLoad('org-a', 'org-b', 4, 4)).toBe(false);
    expect(isCurrentOrganizationLoad('org-b', 'org-b', 3, 4)).toBe(false);
  });

  it('requires confirmation before the member editor removes admin access', () => {
    expect(requiresMemberRoleDowngradeConfirmation('admin', 'member')).toBe(true);
    expect(requiresMemberRoleDowngradeConfirmation('member', 'admin')).toBe(false);
    expect(requiresMemberRoleDowngradeConfirmation('member', 'member')).toBe(false);
  });

  it('requires confirmation for every Project access narrowing', () => {
    const editor = { principal_kind: 'email' as const, principal_value: 'one@example.com', access_role: 'editor' as const };
    const viewer = { ...editor, access_role: 'viewer' as const };
    const secondViewer = { principal_kind: 'email' as const, principal_value: 'two@example.com', access_role: 'viewer' as const };

    expect(requiresProjectAccessNarrowingConfirmation('inherit', [], 'restricted', [viewer])).toBe(true);
    expect(requiresProjectAccessNarrowingConfirmation('inherit', [], 'owner_only', [])).toBe(true);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [viewer], 'owner_only', [])).toBe(true);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [viewer, secondViewer], 'restricted', [viewer])).toBe(true);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [editor], 'restricted', [viewer])).toBe(true);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [viewer], 'restricted', [secondViewer])).toBe(true);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [viewer], 'restricted', [viewer, secondViewer])).toBe(false);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [viewer], 'restricted', [editor])).toBe(false);
    expect(requiresProjectAccessNarrowingConfirmation('restricted', [viewer], 'inherit', [])).toBe(false);
  });

  it('requires confirmation only when a Resource audience shrinks', () => {
    expect(requiresResourceAccessNarrowingConfirmation('public', [], 'scope', ['group-1'])).toBe(true);
    expect(requiresResourceAccessNarrowingConfirmation('public', [], 'private', [])).toBe(true);
    expect(requiresResourceAccessNarrowingConfirmation('scope', ['group-1'], 'private', [])).toBe(true);
    expect(requiresResourceAccessNarrowingConfirmation('scope', ['group-1', 'group-2'], 'scope', ['group-1'])).toBe(true);
    expect(requiresResourceAccessNarrowingConfirmation('private', [], 'scope', ['group-1'])).toBe(false);
    expect(requiresResourceAccessNarrowingConfirmation('scope', ['group-1'], 'public', [])).toBe(false);
    expect(requiresResourceAccessNarrowingConfirmation('scope', ['group-1'], 'scope', ['group-1', 'group-2'])).toBe(false);
  });

  it('leaves object detail routes when switching Organizations', () => {
    expect(organizationSwitchDestination('/admin/organization/groups/group-1')).toBe(
      '/admin/organization/groups',
    );
    expect(organizationSwitchDestination('/admin/organization/instances/instance-1/access')).toBe(
      '/admin/organization/instances',
    );
    expect(organizationSwitchDestination('/admin/organization/instances/instance-1/projects')).toBe(
      '/admin/organization/instances',
    );
    expect(organizationSwitchDestination('/admin/organization/members')).toBeNull();
  });
});
