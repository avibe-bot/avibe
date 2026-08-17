import { describe, expect, it } from 'vitest';

import { isRevisionConflict, OrganizationApiError } from '@/features/organization/api/client';
import {
  buildShowPageAccessPatch,
  classifyShowPageAccessProbe,
  normalizeShowAccessEmail,
  normalizeShowAccessEmails,
  showAccessDraftChanged,
  showAccessTargetEmails,
  showPageHeaderAccess,
  showPageAudienceLabelKey,
  showPageAudienceLevels,
  showPageRestoreAccessDecision,
  showPageSyncPresentation,
  type ShowAccess,
  type ShowPageAccess,
} from './showPageAccess';

const access = (overrides: Partial<ShowPageAccess> = {}): ShowPageAccess => ({
  ok: true,
  mode: 'organization',
  instance_id: 'inst-1',
  organization_id: 'org-1',
  access_level: 'private',
  group_ids: [],
  policy_revision: 4,
  last_applied_control_plane_revision: 4,
  can_use: true,
  can_manage: true,
  can_publish_public: true,
  ...overrides,
});

describe('Show Page access policy helpers', () => {
  it('keeps Personal authenticated access private', () => {
    expect(showPageAudienceLevels('personal')).toEqual(['private']);
  });

  it('offers all three Organization audiences', () => {
    expect(showPageAudienceLevels('organization')).toEqual(['private', 'public', 'scope']);
  });

  it('presents the public wire value as Organization access', () => {
    expect(showPageAudienceLabelKey('public')).toBe('chat.showPage.workspaceLevels.public');
  });

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
  });

  it('keeps emails only for Limited and compares canonical drafts', () => {
    const saved: ShowAccess = {
      page_id: 'ses-1',
      access_mode: 'limited',
      share_id: 'stable-link',
      revision: 4,
      normalized_emails: ['alice@example.com', 'bob@example.com'],
    };
    expect(showAccessTargetEmails('private', saved.normalized_emails)).toEqual([]);
    expect(showAccessTargetEmails('public', saved.normalized_emails)).toEqual([]);
    expect(showAccessTargetEmails('limited', ['bob@example.com', 'alice@example.com'])).toEqual([
      'alice@example.com',
      'bob@example.com',
    ]);
    expect(showAccessDraftChanged(
      saved,
      'limited',
      'stable-link',
      ['bob@example.com', 'alice@example.com'],
    )).toBe(false);
    expect(showAccessDraftChanged(saved, 'public', 'stable-link', [])).toBe(true);
  });

  it('requires at least one group for scoped access and normalizes duplicates', () => {
    expect(buildShowPageAccessPatch('scope', [], 4)).toBeNull();
    expect(buildShowPageAccessPatch('scope', ['group-1', ' group-1 ', 'group-2'], 4)).toEqual({
      access_level: 'scope',
      group_ids: ['group-1', 'group-2'],
      if_match_revision: 4,
    });
  });

  it('keeps local ShowAccess state out of Organization ACL mutations', () => {
    const patch = buildShowPageAccessPatch('public', ['ignored-group'], 4);
    expect(patch).toEqual({
      access_level: 'public',
      group_ids: [],
      if_match_revision: 4,
    });
    expect(patch).not.toHaveProperty('visibility');
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

  it('distinguishes pending, offline, and error sync states', () => {
    expect(showPageSyncPresentation('pending')).toEqual({
      key: 'chat.showPage.workspaceSync.pending',
      tone: 'pending',
    });
    expect(showPageSyncPresentation('offline')).toEqual({
      key: 'chat.showPage.workspaceSync.offline',
      tone: 'pending',
    });
    expect(showPageSyncPresentation('error')).toEqual({
      key: 'chat.showPage.workspaceSync.error',
      tone: 'error',
    });
  });

  it('recognizes a Resource revision conflict without treating generic errors as conflicts', () => {
    expect(isRevisionConflict(new OrganizationApiError(409, {
      error: 'resource_sync_conflict',
      current_revision: 5,
    }))).toBe(true);
    expect(isRevisionConflict(new OrganizationApiError(502, {
      error: 'cloud_management_unavailable',
      retryable: true,
    }))).toBe(false);
  });
});
