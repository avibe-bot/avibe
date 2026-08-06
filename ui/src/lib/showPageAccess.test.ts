import { describe, expect, it } from 'vitest';

import { isRevisionConflict, OrganizationApiError } from '@/features/organization/api/client';
import {
  buildShowPageAccessPatch,
  canChangeShowPagePublicLink,
  classifyShowPageAccessProbe,
  isShowPageVisibilityPayload,
  showPageShareCapabilities,
  showPageHeaderAccess,
  showPageAudienceLabelKey,
  showPageAudienceLevels,
  showPageRestoreAccessDecision,
  showPageSyncPresentation,
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
  public_link_enabled: false,
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

  it('requires at least one group for scoped access and normalizes duplicates', () => {
    expect(buildShowPageAccessPatch('scope', [], 4)).toBeNull();
    expect(buildShowPageAccessPatch('scope', ['group-1', ' group-1 ', 'group-2'], 4)).toEqual({
      access_level: 'scope',
      group_ids: ['group-1', 'group-2'],
      if_match_revision: 4,
    });
  });

  it('keeps Public link state out of Organization ACL mutations', () => {
    const patch = buildShowPageAccessPatch('public', ['ignored-group'], 4);
    expect(patch).toEqual({
      access_level: 'public',
      group_ids: [],
      if_match_revision: 4,
    });
    expect(patch).not.toHaveProperty('public_link_enabled');
    expect(patch).not.toHaveProperty('visibility');
  });

  it('lets managers close a Public link but only resource owners open one', () => {
    const manager = access({ can_publish_public: false, public_link_enabled: true });
    expect(canChangeShowPagePublicLink(manager, false)).toBe(true);
    expect(canChangeShowPagePublicLink(manager, true)).toBe(false);
    expect(canChangeShowPagePublicLink(access(), true)).toBe(true);
  });

  it('lets an access-only manager revoke a Public link without reading its payload', () => {
    const manager = access({
      can_use: false,
      can_publish_public: false,
      public_link_enabled: true,
    });

    expect(showPageShareCapabilities(manager)).toEqual({
      canReadPayload: false,
      canRevokePublicLinkWithoutPayload: true,
      canManageDock: false,
    });
    expect(canChangeShowPagePublicLink(manager, false)).toBe(true);
    expect(canChangeShowPagePublicLink(manager, true)).toBe(false);
  });

  it('fails closed after an access refresh error and keeps Dock authority separate', () => {
    const manager = access({
      can_use: false,
      can_publish_public: false,
      public_link_enabled: true,
    });

    expect(showPageShareCapabilities(manager, { accessInvalid: true })).toEqual({
      canReadPayload: false,
      canRevokePublicLinkWithoutPayload: false,
      canManageDock: false,
    });
    expect(showPageShareCapabilities(null)).toEqual({
      canReadPayload: false,
      canRevokePublicLinkWithoutPayload: false,
      canManageDock: false,
    });
    expect(showPageShareCapabilities(null, { canManageInstance: true }).canReadPayload).toBe(true);
    expect(showPageShareCapabilities(access(), { canManageInstance: false }).canManageDock).toBe(false);
    expect(showPageShareCapabilities(access(), { canManageInstance: true }).canManageDock).toBe(true);
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

  it('distinguishes metadata-only visibility results from readable page payloads', () => {
    expect(isShowPageVisibilityPayload({
      ok: true,
      public_link_enabled: false,
    })).toBe(false);
    expect(isShowPageVisibilityPayload({
      ok: true,
      session_id: 'session-1',
      visibility: 'private',
      active_url: '/show/session-1/',
      share_id: null,
    })).toBe(true);
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
