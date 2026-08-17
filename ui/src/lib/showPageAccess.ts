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
  can_use: boolean;
  can_manage: boolean;
  can_publish_public: boolean;
};

export type ShowPageAccessProbe =
  | { status: 'granted'; access: ShowPageAccess }
  | { status: 'denied'; access: null }
  | { status: 'error'; access: null };

export type ShowAccessMode = 'private' | 'limited' | 'public';

export type ShowAccess = {
  page_id: string;
  access_mode: ShowAccessMode;
  share_id: string | null;
  revision: number;
  normalized_emails: string[];
};

export type ShowAccessSettingsResult = {
  show_access: ShowAccess;
};

export type ShowAccessApplyRequest = {
  expected_revision: number;
  target_access_mode: ShowAccessMode;
  target_share_id: string | null;
  target_emails: string[];
};

export type ShowAccessApplyResult = {
  status: 'applied' | 'no_change' | 'conflict' | 'share_id_taken' | 'invalid';
  show_access: ShowAccess;
};

export const SHOW_ACCESS_EMAIL_MAX_COUNT = 64;
const SHOW_ACCESS_EMAIL_PATTERN = /^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;
const ASCII_SURROUNDING_WHITESPACE = /^[\t\n\f\r\v ]+|[\t\n\f\r\v ]+$/g;

export function normalizeShowAccessEmail(raw: string): string | null {
  const normalized = raw
    .replace(ASCII_SURROUNDING_WHITESPACE, '')
    .replace(/[A-Z]/g, (value) => value.toLowerCase());
  return normalized.length <= 320 && SHOW_ACCESS_EMAIL_PATTERN.test(normalized)
    ? normalized
    : null;
}

export function normalizeShowAccessEmails(emails: string[]): string[] | null {
  if (emails.length > SHOW_ACCESS_EMAIL_MAX_COUNT) return null;
  const normalized = emails.map(normalizeShowAccessEmail);
  if (normalized.some((email) => email === null)) return null;
  return [...new Set(normalized as string[])].sort();
}

export function showAccessTargetEmails(
  mode: ShowAccessMode,
  emails: string[],
): string[] {
  return mode === 'limited' ? [...new Set(emails)].sort() : [];
}

export function showAccessDraftChanged(
  saved: ShowAccess,
  mode: ShowAccessMode,
  shareId: string | null,
  emails: string[],
): boolean {
  return saved.access_mode !== mode
    || saved.share_id !== shareId
    || saved.normalized_emails.join('\u0000') !== showAccessTargetEmails(mode, emails).join('\u0000');
}

function isShowPageAccess(value: unknown): value is ShowPageAccess {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<ShowPageAccess>;
  return candidate.ok === true
    && (candidate.mode === 'personal' || candidate.mode === 'organization')
    && typeof candidate.can_use === 'boolean'
    && typeof candidate.can_manage === 'boolean'
    && typeof candidate.can_publish_public === 'boolean';
}

export function classifyShowPageAccessProbe(
  status: number,
  payload: unknown,
): ShowPageAccessProbe {
  if (status >= 200 && status < 300 && isShowPageAccess(payload)) {
    return { status: 'granted', access: payload };
  }
  if (status === 403 || status === 404) {
    return { status: 'denied', access: null };
  }
  return { status: 'error', access: null };
}

export function showPageRestoreAccessDecision(
  canManageInstance: boolean,
  probe: ShowPageAccessProbe | null,
): 'allow' | 'deny' | 'wait' {
  if (canManageInstance) return 'allow';
  if (!probe || probe.status === 'error') return 'wait';
  if (probe.status === 'denied') return 'deny';
  return probe.access.can_use ? 'allow' : 'deny';
}

export function showPageHeaderAccess(
  canManageInstance: boolean,
  access: ShowPageAccess | null,
): { canOpen: boolean; canManage: boolean } {
  return {
    canOpen: canManageInstance || access?.can_use === true,
    canManage: canManageInstance || access?.can_manage === true,
  };
}

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
