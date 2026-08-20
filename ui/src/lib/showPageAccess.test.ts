import { describe, expect, it } from 'vitest';

import {
  SHOW_ACCESS_ENTRY_MAX_COUNT,
  SHOW_ACCESS_SUGGESTION_LIMIT,
  classifyShowPageAccessProbe,
  normalizeShowAccessEmail,
  normalizeShowAccessEmails,
  showAccessApplyPayload,
  showAccessDirectoryOf,
  showAccessDraftChanged,
  showAccessWireChanged,
  showAccessWithLocalExtras,
  showAccessEntriesOf,
  showAccessSuggestions,
  showAccessTargetEmails,
  showAccessTargetEntries,
  showPageHeaderAccess,
  showPageRestoreAccessDecision,
  withShowAccessEntry,
  withoutShowAccessEntry,
  type ShowAccess,
  type ShowAccessDirectory,
  type ShowPageAccess,
} from './showPageAccess';
import type { PermissionsResponse } from '@/features/permissions/types';

const access = (overrides: Partial<ShowPageAccess> = {}): ShowPageAccess => ({
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

const directory: ShowAccessDirectory = {
  organization_id: 'org-1',
  organization_name: 'Acme',
  groups: [
    { id: 'grp-design', name: 'Design' },
    { id: 'grp-eng', name: 'Engineering' },
  ],
  emails: ['alice@example.com', 'bob@example.com'],
};

describe('Show Page access policy helpers', () => {
  it('normalizes ShowAccess emails with the frozen ASCII contract', () => {
    expect(normalizeShowAccessEmail(' Guest+Demo@Example.COM ')).toBe(
      'guest+demo@example.com',
    );
    expect(normalizeShowAccessEmails([
      ' Bob@Example.com ',
      'alice@example.com',
      'bob@example.com',
    ])).toEqual(['alice@example.com', 'bob@example.com']);
    for (const invalid of [
      '.guest@example.com',
      'guest.@example.com',
      'guest..name@example.com',
      'guest@-example.com',
      'guest@example-.com',
      'guest@example..com',
      'guest@@example.com',
    ]) {
      expect(normalizeShowAccessEmail(invalid)).toBeNull();
    }
    expect(normalizeShowAccessEmails(
      Array.from({ length: SHOW_ACCESS_ENTRY_MAX_COUNT + 1 }, (_, index) => (
        `guest-${index}@example.com`
      )),
    )).toBeNull();
  });

  it('reads a pre-heterogeneous audience as email entries', () => {
    const saved: ShowAccess = {
      page_id: 'ses-1',
      access_mode: 'limited',
      share_id: 'stable-link',
      revision: 4,
      normalized_emails: ['bob@example.com', 'alice@example.com'],
    };
    expect(showAccessEntriesOf(saved)).toEqual([
      { kind: 'email', value: 'alice@example.com' },
      { kind: 'email', value: 'bob@example.com' },
    ]);
    expect(showAccessEntriesOf({
      ...saved,
      access_entries: [
        { kind: 'email', value: 'bob@example.com' },
        { kind: 'organization', value: 'org-1' },
        { kind: 'group', value: 'grp-eng' },
      ],
    })).toEqual([
      { kind: 'organization', value: 'org-1' },
      { kind: 'group', value: 'grp-eng' },
      { kind: 'email', value: 'bob@example.com' },
    ]);
  });

  it('keeps email, group, and Organization entries as peers', () => {
    const entries = withShowAccessEntry(
      withShowAccessEntry(
        [{ kind: 'email' as const, value: 'alice@example.com' }],
        { kind: 'group', value: 'grp-eng' },
      ),
      { kind: 'organization', value: 'org-1' },
    );
    // Adding the Organization neither absorbs nor supersedes the narrower entries.
    expect(entries).toEqual([
      { kind: 'organization', value: 'org-1' },
      { kind: 'group', value: 'grp-eng' },
      { kind: 'email', value: 'alice@example.com' },
    ]);
    // At most one Organization entry can exist, so a second one replaces it.
    expect(withShowAccessEntry(entries, { kind: 'organization', value: 'org-2' })
      .filter((entry) => entry.kind === 'organization')).toEqual([
      { kind: 'organization', value: 'org-2' },
    ]);
    expect(withoutShowAccessEntry(entries, { kind: 'organization', value: 'org-1' })).toEqual([
      { kind: 'group', value: 'grp-eng' },
      { kind: 'email', value: 'alice@example.com' },
    ]);
  });

  it('sends the audience only for Limited and compares canonical drafts', () => {
    const entries = [
      { kind: 'email' as const, value: 'bob@example.com' },
      { kind: 'organization' as const, value: 'org-1' },
      { kind: 'email' as const, value: 'alice@example.com' },
    ];
    const saved: ShowAccess = {
      page_id: 'ses-1',
      access_mode: 'limited',
      share_id: 'stable-link',
      revision: 4,
      normalized_emails: ['alice@example.com', 'bob@example.com'],
      access_entries: [
        { kind: 'organization', value: 'org-1' },
        { kind: 'email', value: 'alice@example.com' },
        { kind: 'email', value: 'bob@example.com' },
      ],
    };
    expect(showAccessTargetEntries('private', entries)).toEqual([]);
    expect(showAccessTargetEntries('public', entries)).toEqual([]);
    expect(showAccessTargetEmails('limited', entries)).toEqual([
      'alice@example.com',
      'bob@example.com',
    ]);
    expect(showAccessTargetEmails('private', entries)).toEqual([]);
    expect(showAccessDraftChanged(saved, 'limited', 'stable-link', entries)).toBe(false);
    expect(showAccessDraftChanged(saved, 'public', 'stable-link', entries)).toBe(true);
    expect(showAccessDraftChanged(
      saved,
      'limited',
      'stable-link',
      withoutShowAccessEntry(entries, { kind: 'organization', value: 'org-1' }),
    )).toBe(true);
    // Until A1/A2 land, the apply route 400s on any extra key, so the wire
    // form is the five-key set with only the email projection.
    expect(showAccessApplyPayload(4, 'limited', 'stable-link', entries)).toEqual({
      expected_revision: 4,
      target_access_mode: 'limited',
      target_share_id: 'stable-link',
      target_emails: ['alice@example.com', 'bob@example.com'],
    });
    expect(Object.keys(showAccessApplyPayload(4, 'limited', 'stable-link', entries))).toEqual([
      'expected_revision',
      'target_access_mode',
      'target_share_id',
      'target_emails',
    ]);
    expect(showAccessWireChanged(saved, 'limited', 'stable-link', entries)).toBe(false);
    expect(showAccessWireChanged(
      saved,
      'limited',
      'stable-link',
      withoutShowAccessEntry(entries, { kind: 'organization', value: 'org-1' }),
    )).toBe(false);
    expect(showAccessWireChanged(
      saved,
      'limited',
      'stable-link',
      withoutShowAccessEntry(entries, { kind: 'email', value: 'alice@example.com' }),
    )).toBe(true);
    expect(showAccessWithLocalExtras(
      { ...saved, access_entries: undefined },
      entries,
    )).toEqual([
      { kind: 'organization', value: 'org-1' },
      { kind: 'email', value: 'alice@example.com' },
      { kind: 'email', value: 'bob@example.com' },
    ]);
  });

  it('derives the audience directory only for an Organization Avibe', () => {
    const permissions = (organization: { id: string; name: string } | null) => ({
      ok: true,
      source: 'live',
      offline: false,
      cached_at: null,
      projection: {
        schema_version: 1,
        instance: {
          id: 'inst-1',
          organization,
          access_mode: 'allowlist',
          permission_authority: 'cloud',
          local_mutation_allowed: false,
          authorization_revision: 3,
        },
        capabilities: [],
        access: { owner: { email: null, role: 'owner' }, entries: [] },
        directory: {
          members: [
            { id: 'u1', email: 'Alice@Example.com', organization_role: 'member', group_ids: [] },
            { id: 'u2', email: 'not-an-email', organization_role: 'member', group_ids: [] },
          ],
          groups: [
            { id: 'grp-eng', name: 'Engineering', archived_at: null },
            { id: 'grp-old', name: 'Legacy', archived_at: '2026-01-01T00:00:00Z' },
          ],
        },
        projects: [],
        policy_sync: { status: 'in_sync', projects: {}, resources: {} },
      },
    } as unknown as PermissionsResponse);

    expect(showAccessDirectoryOf(permissions({ id: 'org-1', name: 'Acme' }))).toEqual({
      organization_id: 'org-1',
      organization_name: 'Acme',
      groups: [{ id: 'grp-eng', name: 'Engineering' }],
      emails: ['alice@example.com'],
    });
    // A Personal Avibe has no Organization, which is what hides the Organization
    // toggle and group search.
    expect(showAccessDirectoryOf(permissions(null))).toBeNull();
  });

  it('searches groups and people, and never re-offers a selected entry', () => {
    expect(showAccessSuggestions(null, '', [])).toEqual({ suggestions: [], truncated: false });
    expect(showAccessSuggestions(directory, '', []).suggestions).toEqual([
      { kind: 'group', value: 'grp-design', label: 'Design' },
      { kind: 'group', value: 'grp-eng', label: 'Engineering' },
      { kind: 'email', value: 'alice@example.com', label: 'alice@example.com' },
      { kind: 'email', value: 'bob@example.com', label: 'bob@example.com' },
    ]);
    // A partial query narrows both kinds.
    expect(showAccessSuggestions(directory, 'eng', []).suggestions).toEqual([
      { kind: 'group', value: 'grp-eng', label: 'Engineering' },
    ]);
    expect(showAccessSuggestions(directory, 'ali', []).suggestions).toEqual([
      { kind: 'email', value: 'alice@example.com', label: 'alice@example.com' },
    ]);
    expect(showAccessSuggestions(directory, '', [
      { kind: 'group', value: 'grp-eng' },
      { kind: 'email', value: 'alice@example.com' },
    ]).suggestions).toEqual([
      { kind: 'group', value: 'grp-design', label: 'Design' },
      { kind: 'email', value: 'bob@example.com', label: 'bob@example.com' },
    ]);

    const crowded = showAccessSuggestions({
      ...directory,
      emails: Array.from({ length: 20 }, (_, index) => `guest-${index}@example.com`),
    }, '', []);
    expect(crowded.suggestions).toHaveLength(SHOW_ACCESS_SUGGESTION_LIMIT);
    // Groups keep their slots even against a long member list.
    expect(crowded.suggestions.filter((option) => option.kind === 'group')).toHaveLength(2);
    expect(crowded.truncated).toBe(true);
  });

  it('separates page use from page-specific access management in the chat header', () => {
    expect(showPageHeaderAccess(false, access({ can_use: true, can_manage: false }))).toEqual({
      canOpen: true,
      canManage: false,
    });
    expect(showPageHeaderAccess(false, access({ can_use: false, can_manage: true }))).toEqual({
      canOpen: false,
      canManage: true,
    });
    expect(showPageHeaderAccess(false, null)).toEqual({ canOpen: false, canManage: false });
    expect(showPageHeaderAccess(true, null)).toEqual({ canOpen: true, canManage: true });
  });

  it('keeps transient access probe failures distinct from definitive denials', () => {
    const granted = classifyShowPageAccessProbe(200, access());
    const managerOnly = classifyShowPageAccessProbe(200, access({ can_use: false }));
    const denied = classifyShowPageAccessProbe(403, { error: 'resource_access_forbidden' });
    const missing = classifyShowPageAccessProbe(404, { error: 'show_page_not_found' });
    const unavailable = classifyShowPageAccessProbe(503, { error: 'backend_unavailable' });

    expect(showPageRestoreAccessDecision(false, granted)).toBe('allow');
    expect(showPageRestoreAccessDecision(false, managerOnly)).toBe('deny');
    expect(showPageRestoreAccessDecision(false, denied)).toBe('deny');
    expect(showPageRestoreAccessDecision(false, missing)).toBe('deny');
    expect(showPageRestoreAccessDecision(false, unavailable)).toBe('wait');
    expect(showPageRestoreAccessDecision(false, { status: 'error', access: null })).toBe('wait');
    expect(showPageRestoreAccessDecision(true, null)).toBe('allow');
  });

});
