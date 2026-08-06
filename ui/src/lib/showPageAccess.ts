import type { ResourceAccessLevel, SyncStatus } from '@/features/organization/api/types';

export type ShowPageAccess = {
  ok: true;
  mode: 'personal' | 'organization';
  instance_id: string | null;
  organization_id: string | null;
  access_level: ResourceAccessLevel;
  group_ids: string[];
  policy_revision: number | null;
  last_applied_control_plane_revision: number | null;
  can_manage: boolean;
  can_publish_public: boolean;
  public_link_enabled: boolean;
};

export type ShowPageAccessPatch = {
  access_level: ResourceAccessLevel;
  group_ids: string[];
  if_match_revision: number;
};

export const showPageAudienceLevels = (
  mode: ShowPageAccess['mode'],
): ResourceAccessLevel[] => (
  mode === 'organization' ? ['private', 'public', 'scope'] : ['private']
);

export const showPageAudienceLabelKey = (level: ResourceAccessLevel): string => (
  `chat.showPage.workspaceLevels.${level}`
);

export function buildShowPageAccessPatch(
  level: ResourceAccessLevel,
  groupIds: string[],
  revision: number,
): ShowPageAccessPatch | null {
  const normalizedGroupIds = level === 'scope'
    ? [...new Set(groupIds.map((id) => id.trim()).filter(Boolean))]
    : [];
  if (level === 'scope' && normalizedGroupIds.length === 0) return null;
  return {
    access_level: level,
    group_ids: normalizedGroupIds,
    if_match_revision: revision,
  };
}

export function canChangeShowPagePublicLink(
  access: ShowPageAccess | null,
  nextEnabled: boolean,
): boolean {
  if (!access) return false;
  return nextEnabled ? access.can_publish_public : access.can_manage;
}

export function showPageSyncPresentation(status: SyncStatus): {
  key: string;
  tone: 'muted' | 'pending' | 'error';
} | null {
  if (status === 'in_sync' || status === 'none' || status === 'deleted') return null;
  if (status === 'error') {
    return { key: 'chat.showPage.workspaceSync.error', tone: 'error' };
  }
  if (status === 'offline') {
    return { key: 'chat.showPage.workspaceSync.offline', tone: 'pending' };
  }
  return { key: 'chat.showPage.workspaceSync.pending', tone: 'pending' };
}
