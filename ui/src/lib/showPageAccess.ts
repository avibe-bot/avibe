import type { ResourceAccessLevel, SyncStatus } from '@/features/organization/api/types';
import type { ShowPageLinkInfo } from './showPageLinks';

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
  public_link_enabled: boolean;
};

export type ShowPageAccessProbe =
  | { status: 'granted'; access: ShowPageAccess }
  | { status: 'denied'; access: null }
  | { status: 'error'; access: null };

export type ShowPageVisibilityMetadata = {
  ok: true;
  public_link_enabled: boolean;
};

export type ShowPageVisibilityPayload<Payload extends ShowPageLinkInfo = ShowPageLinkInfo> = {
  ok: true;
} & Payload;
export type ShowPageVisibilityResult<Payload extends ShowPageLinkInfo = ShowPageLinkInfo> =
  | ShowPageVisibilityPayload<Payload>
  | ShowPageVisibilityMetadata;

export type ShowPageAuthorizedEmails = {
  ok: true;
  emails: string[];
  changed?: boolean;
};

const SHOW_PAGE_EMAIL_PATTERN = /^[a-z0-9._%+-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$/;

export function normalizeShowPageAuthorizedEmail(raw: string): string | null {
  const normalized = raw.trim().toLowerCase();
  return SHOW_PAGE_EMAIL_PATTERN.test(normalized) ? normalized : null;
}

export function requiresShowPageEmailRevocationConfirmation(
  savedEmails: string[],
  nextEmails: string[],
): boolean {
  const next = new Set(nextEmails);
  return savedEmails.some((email) => !next.has(email));
}

function isShowPageAccess(value: unknown): value is ShowPageAccess {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<ShowPageAccess>;
  return candidate.ok === true
    && (candidate.mode === 'personal' || candidate.mode === 'organization')
    && typeof candidate.can_use === 'boolean'
    && typeof candidate.can_manage === 'boolean'
    && typeof candidate.can_publish_public === 'boolean'
    && typeof candidate.public_link_enabled === 'boolean';
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

export function isShowPageVisibilityPayload<Payload extends ShowPageLinkInfo>(
  result: ShowPageVisibilityResult<Payload>,
): result is ShowPageVisibilityPayload<Payload> {
  return 'session_id' in result && typeof result.session_id === 'string';
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

export function canChangeShowPagePublicLink(
  access: ShowPageAccess | null,
  nextEnabled: boolean,
): boolean {
  if (!access) return false;
  return nextEnabled ? access.can_publish_public : access.can_manage;
}

export function showPageShareCapabilities(
  access: ShowPageAccess | null,
  options: {
    accessInvalid?: boolean;
    canManageInstance?: boolean;
  } = {},
): {
  canReadPayload: boolean;
  canRevokePublicLinkWithoutPayload: boolean;
  canManageDock: boolean;
} {
  const accessValid = options.accessInvalid !== true;
  const canManageInstance = options.canManageInstance === true;
  return {
    canReadPayload: accessValid && (canManageInstance || access?.can_use === true),
    canRevokePublicLinkWithoutPayload: Boolean(
      accessValid
      && access
      && !access.can_use
      && access.can_manage
      && access.public_link_enabled,
    ),
    // Dock writes remain Instance-owner operations. Page-level use/manage
    // authority deliberately does not inherit this independent capability.
    canManageDock: canManageInstance,
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
