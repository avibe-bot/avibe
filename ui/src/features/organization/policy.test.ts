import { describe, expect, it } from 'vitest';

import {
  aggregateSyncStatus,
  hasDuplicateProjectPrincipals,
  isCurrentOrganizationLoad,
  normalizeOrganizationPrincipal,
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
});
