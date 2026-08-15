import type { RouteHopRef, Source, SourcePatch, SupplyGap } from './types';
import { SOURCE_DISPLAY_NAME_MAX_LENGTH } from './types';
import { optionalTrimmedTextWithin } from './validation';

/** Management changes a valid Source; repair restores a blocked one. */
export type ManageKind = 'edit_source' | 'delete_source';

export type ManageDestination = 'edit_source_dialog' | 'delete_source_guard';

export const MANAGE_DESTINATION: Record<ManageKind, ManageDestination> = {
  edit_source: 'edit_source_dialog',
  delete_source: 'delete_source_guard',
};

export const MANAGE_LABEL_KEY: Record<ManageKind, string> = {
  edit_source: 'settings.models.sourceDetail.manage.edit',
  delete_source: 'settings.models.sourceDetail.manage.remove',
};

/** Every persisted Source has the same two management actions. */
export const manageActions = (): ManageKind[] => Object.keys(MANAGE_DESTINATION) as ManageKind[];

/** Only Avibe-held API-key sources own an endpoint that this editor can change. */
export const canEditSourceEndpoint = (source: Source): boolean =>
  source.kind === 'api_key' && source.supply_channel === 'hub' && Boolean(source.credential_ref);

export type SourceEditDraft = {
  displayName: string;
  baseUrl: string;
};

export type ManageGuardPlan = { hops: RouteHopRef[]; gaps: SupplyGap[] };

export const MANAGE_STAGE_KINDS = [
  'idle',
  'editing',
  'submitting_edit',
  'confirming_edit',
  'edit_failed',
  'confirming_delete',
  'submitting_delete',
  'delete_failed',
  'committed_edit_impact',
  'committed_delete_impact',
] as const;
export type ManageStageKind = (typeof MANAGE_STAGE_KINDS)[number];

export type ManageStage =
  | { kind: 'idle' }
  | { kind: 'editing'; draft: SourceEditDraft }
  | { kind: 'submitting_edit'; draft: SourceEditDraft; patch: SourcePatch; plan: ManageGuardPlan | null; forced: boolean; surface: 'edit' | 'guard' }
  | { kind: 'confirming_edit'; draft: SourceEditDraft; patch: SourcePatch; plan: ManageGuardPlan }
  | { kind: 'edit_failed'; draft: SourceEditDraft; patch: SourcePatch; plan: ManageGuardPlan | null; forced: boolean; retryRead: boolean; before: Source }
  | { kind: 'confirming_delete'; plan: ManageGuardPlan | null }
  | { kind: 'submitting_delete'; plan: ManageGuardPlan | null; forced: boolean }
  | { kind: 'delete_failed'; plan: ManageGuardPlan | null; forced: boolean; retryRead: boolean }
  | { kind: 'committed_edit_impact'; plan: ManageGuardPlan; complete: () => Promise<void> }
  | { kind: 'committed_delete_impact'; plan: ManageGuardPlan; complete: () => Promise<void> };

type ManageStageDestination = ManageStageKind | 'none' | 'blocked' | 'complete';
export type ManageFailureSurface = 'none' | 'edit_dialog' | 'guard_dialog' | 'delete_inline' | 'committed_dialog';

/** Total transition metadata: a new stage cannot omit its exit or failure owner. */
export const MANAGE_STAGE_CANCEL: Record<ManageStageKind, ManageStageDestination> = {
  idle: 'none',
  editing: 'idle',
  submitting_edit: 'blocked',
  confirming_edit: 'editing',
  edit_failed: 'idle',
  confirming_delete: 'idle',
  submitting_delete: 'blocked',
  delete_failed: 'idle',
  committed_edit_impact: 'complete',
  committed_delete_impact: 'complete',
};

export const MANAGE_STAGE_RETRY: Record<ManageStageKind, ManageStageDestination> = {
  idle: 'none',
  editing: 'submitting_edit',
  submitting_edit: 'none',
  confirming_edit: 'submitting_edit',
  edit_failed: 'submitting_edit',
  confirming_delete: 'submitting_delete',
  submitting_delete: 'none',
  delete_failed: 'submitting_delete',
  committed_edit_impact: 'none',
  committed_delete_impact: 'none',
};

export const MANAGE_STAGE_FAILURE_SURFACE: Record<ManageStageKind, ManageFailureSurface> = {
  idle: 'none',
  editing: 'edit_dialog',
  submitting_edit: 'edit_dialog',
  confirming_edit: 'guard_dialog',
  edit_failed: 'edit_dialog',
  confirming_delete: 'guard_dialog',
  submitting_delete: 'delete_inline',
  delete_failed: 'delete_inline',
  committed_edit_impact: 'committed_dialog',
  committed_delete_impact: 'committed_dialog',
};

export type SourceEditInvalidReason =
  | 'displayNameRequired'
  | 'displayNameTooLong'
  | 'displayNameCredential'
  | 'baseUrlInvalid'
  | 'baseUrlCredential';

export const SOURCE_EDIT_REASON_KEY: Record<SourceEditInvalidReason, string> = {
  displayNameRequired: 'settings.models.sourceDetail.edit.validation.displayNameRequired',
  displayNameTooLong: 'settings.models.sourceDetail.edit.validation.displayNameTooLong',
  displayNameCredential: 'settings.models.sourceDetail.edit.validation.displayNameCredential',
  baseUrlInvalid: 'settings.models.sourceDetail.edit.validation.baseUrlInvalid',
  baseUrlCredential: 'settings.models.sourceDetail.edit.validation.baseUrlCredential',
};

export type SourceEditAssessment =
  | { valid: true; patch: SourcePatch | null; reason: null }
  | { valid: false; patch: null; reason: SourceEditInvalidReason };

// These two overlapping sets intentionally mirror config/v2_config.py. They
// stay separate until the server contract changes with the shared fixture.
const CREDENTIAL_QUERY_KEYS = new Set([
  'access_token',
  'api_key',
  'apikey',
  'auth',
  'authorization',
  'bearer',
  'credential',
  'key',
  'password',
  'passwd',
  'secret',
  'sig',
  'signature',
  'token',
]);

const CREDENTIAL_QUERY_MARKERS = [
  'api_key',
  'access_token',
  'auth_token',
  'token',
  'authorization',
  'signature',
  'secret',
  'password',
  'credential',
];

const CREDENTIAL_PATTERNS = [
  /\b(?:sk|rk|pk|sess|token)[-_][a-z0-9_-]{8,}\b/i,
  /\b(?:authorization|api[_ -]?key|access[_ -]?token)\s*[:=]\s*(?:sk[-_][a-z0-9_-]{8,}|[a-z0-9._~+/=-]{16,})/i,
  /\bbearer\s+[a-z0-9._~+/=-]{8,}/i,
];

const containsCredentialMaterial = (value: string): boolean =>
  CREDENTIAL_PATTERNS.some((pattern) => pattern.test(JSON.stringify(value)));

type BaseUrlAssessment =
  | { valid: true; value: string | null }
  | { valid: false; reason: Extract<SourceEditInvalidReason, `baseUrl${string}`> };

const assessedEditedBaseUrl = (value: string): BaseUrlAssessment => {
  if (!value) return { valid: true, value: null };
  if (containsCredentialMaterial(value)) return { valid: false, reason: 'baseUrlCredential' };

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return { valid: false, reason: 'baseUrlInvalid' };
  }
  if (
    !['http:', 'https:'].includes(parsed.protocol.toLowerCase())
    || !parsed.hostname
    || parsed.username
    || parsed.password
    || parsed.hash
  ) return { valid: false, reason: 'baseUrlInvalid' };

  for (const key of parsed.searchParams.keys()) {
    const normalized = key.trim().toLowerCase().replace(/[-.]/g, '_');
    if (
      CREDENTIAL_QUERY_KEYS.has(normalized)
      || CREDENTIAL_QUERY_MARKERS.some((marker) => normalized.includes(marker))
    ) return { valid: false, reason: 'baseUrlCredential' };
  }

  // This is a user-facing safety pre-filter, not the URL authority. The server
  // validates and normalizes the value that is sent verbatim from here.
  return { valid: true, value };
};

/** Pre-filter an actual edit and diff without claiming server target policy. */
export const assessSourceEdit = (source: Source, draft: SourceEditDraft): SourceEditAssessment => {
  const displayName = draft.displayName.trim();
  if (!displayName) return { valid: false, patch: null, reason: 'displayNameRequired' };
  if (!optionalTrimmedTextWithin(draft.displayName, SOURCE_DISPLAY_NAME_MAX_LENGTH)) {
    return { valid: false, patch: null, reason: 'displayNameTooLong' };
  }
  if (containsCredentialMaterial(displayName)) {
    return { valid: false, patch: null, reason: 'displayNameCredential' };
  }

  const patch: SourcePatch = {};
  if (displayName !== source.display_name) patch.display_name = displayName;

  if (canEditSourceEndpoint(source)) {
    const baseUrl = draft.baseUrl.trim();
    if (baseUrl !== (source.base_url ?? '')) {
      const endpoint = assessedEditedBaseUrl(baseUrl);
      if (!endpoint.valid) return { valid: false, patch: null, reason: endpoint.reason };
      patch.base_url = endpoint.value;
    }
  }

  return { valid: true, patch: Object.keys(patch).length > 0 ? patch : null, reason: null };
};
