import type { RouteHopRef, Source, SourcePatch, SupplyGap } from './types';
import { SOURCE_DISPLAY_NAME_MAX_LENGTH } from './types';
import type { SourceMutationLanding } from './mutationSettlement';
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
  | { kind: 'delete_failed'; plan: ManageGuardPlan | null; forced: boolean; retryRead: boolean; before: Source }
  | { kind: 'committed_edit_impact'; plan: ManageGuardPlan; complete: () => Promise<SourceMutationLanding>; landingFailed: boolean }
  | { kind: 'committed_delete_impact'; plan: ManageGuardPlan; complete: () => Promise<SourceMutationLanding>; landingFailed: boolean };

export type ManageFailureSurface = 'none' | 'edit_dialog' | 'guard_dialog' | 'delete_inline' | 'committed_dialog';

type ManageStageTransition<Event> = {
  [Kind in ManageStageKind]: (
    stage: Extract<ManageStage, { kind: Kind }>,
    event: Event,
  ) => ManageStage;
};

type CancelManageStage = { type: 'cancel' };
type RetryManageStage = { type: 'retry' };
type EditManageDraft = { type: 'edit_draft'; draft: SourceEditDraft };
type DismissUnresolvedManageStage = { type: 'dismiss_unresolved' };
type LandManageStage = { type: 'land'; verdict: SourceMutationLanding };

const keepManageStage = <Stage extends ManageStage>(stage: Stage): Stage => stage;
const idleManageStage = (): ManageStage => ({ kind: 'idle' });

/** Total event authority: a new stage cannot omit any user-driven transition. */
export const MANAGE_STAGE_CANCEL = {
  idle: keepManageStage,
  editing: idleManageStage,
  submitting_edit: keepManageStage,
  confirming_edit: (stage) => ({ kind: 'editing', draft: stage.draft }),
  edit_failed: (stage) => stage.retryRead ? stage : idleManageStage(),
  confirming_delete: idleManageStage,
  submitting_delete: keepManageStage,
  delete_failed: (stage) => stage.retryRead ? stage : idleManageStage(),
  committed_edit_impact: keepManageStage,
  committed_delete_impact: keepManageStage,
} satisfies ManageStageTransition<CancelManageStage>;

export const MANAGE_STAGE_RETRY = {
  idle: keepManageStage,
  editing: keepManageStage,
  submitting_edit: keepManageStage,
  confirming_edit: (stage) => ({
    kind: 'submitting_edit',
    draft: stage.draft,
    patch: stage.patch,
    plan: stage.plan,
    forced: true,
    surface: 'guard',
  }),
  edit_failed: (stage) => ({
    kind: 'submitting_edit',
    draft: stage.draft,
    patch: stage.patch,
    plan: stage.plan,
    forced: stage.forced,
    surface: 'edit',
  }),
  confirming_delete: (stage) => ({
    kind: 'submitting_delete',
    plan: stage.plan,
    forced: stage.plan !== null,
  }),
  submitting_delete: keepManageStage,
  delete_failed: (stage) => ({
    kind: 'submitting_delete',
    plan: stage.plan,
    forced: stage.forced,
  }),
  committed_edit_impact: keepManageStage,
  committed_delete_impact: keepManageStage,
} satisfies ManageStageTransition<RetryManageStage>;

export const MANAGE_STAGE_EDIT_DRAFT = {
  idle: keepManageStage,
  editing: (stage, event) => ({ ...stage, draft: event.draft }),
  submitting_edit: keepManageStage,
  confirming_edit: keepManageStage,
  edit_failed: (stage, event) => ({ ...stage, draft: event.draft }),
  confirming_delete: keepManageStage,
  submitting_delete: keepManageStage,
  delete_failed: keepManageStage,
  committed_edit_impact: keepManageStage,
  committed_delete_impact: keepManageStage,
} satisfies ManageStageTransition<EditManageDraft>;

export const MANAGE_STAGE_DISMISS_UNRESOLVED = {
  idle: keepManageStage,
  editing: keepManageStage,
  submitting_edit: keepManageStage,
  confirming_edit: keepManageStage,
  edit_failed: (stage) => stage.retryRead ? idleManageStage() : stage,
  confirming_delete: keepManageStage,
  submitting_delete: keepManageStage,
  delete_failed: (stage) => stage.retryRead ? idleManageStage() : stage,
  committed_edit_impact: (stage) => stage.landingFailed ? idleManageStage() : stage,
  committed_delete_impact: (stage) => stage.landingFailed ? idleManageStage() : stage,
} satisfies ManageStageTransition<DismissUnresolvedManageStage>;

export const MANAGE_STAGE_LANDING = {
  idle: keepManageStage,
  editing: keepManageStage,
  submitting_edit: keepManageStage,
  confirming_edit: keepManageStage,
  edit_failed: keepManageStage,
  confirming_delete: keepManageStage,
  submitting_delete: keepManageStage,
  delete_failed: keepManageStage,
  committed_edit_impact: (stage, event) => event.verdict === 'landed'
    ? idleManageStage()
    : { ...stage, landingFailed: true },
  committed_delete_impact: (stage, event) => event.verdict === 'landed'
    ? idleManageStage()
    : { ...stage, landingFailed: true },
} satisfies ManageStageTransition<LandManageStage>;

export type ManageStageEvent =
  | { type: 'begin_edit'; draft: SourceEditDraft }
  | { type: 'begin_delete' }
  | { type: 'submit_edit'; draft: SourceEditDraft; patch: SourcePatch; plan: ManageGuardPlan | null; surface: 'edit' | 'guard' }
  | { type: 'submit_delete'; plan: ManageGuardPlan | null }
  | { type: 'guard_edit'; draft: SourceEditDraft; patch: SourcePatch; plan: ManageGuardPlan }
  | { type: 'guard_delete'; plan: ManageGuardPlan }
  | { type: 'fail_edit'; draft: SourceEditDraft; patch: SourcePatch; plan: ManageGuardPlan | null; forced: boolean; retryRead: boolean; before: Source }
  | { type: 'fail_delete'; plan: ManageGuardPlan | null; forced: boolean; retryRead: boolean; before: Source }
  | { type: 'commit_impact'; action: 'edit' | 'delete'; plan: ManageGuardPlan; complete: () => Promise<SourceMutationLanding> }
  | { type: 'settled' }
  | CancelManageStage
  | RetryManageStage
  | EditManageDraft
  | DismissUnresolvedManageStage
  | LandManageStage;

const applyManageStageTransition = <Event,>(
  transitions: ManageStageTransition<Event>,
  stage: ManageStage,
  event: Event,
): ManageStage => {
  const transition = transitions[stage.kind] as (current: ManageStage, currentEvent: Event) => ManageStage;
  return transition(stage, event);
};

export const transitionManageStage = (stage: ManageStage, event: ManageStageEvent): ManageStage => {
  switch (event.type) {
    case 'begin_edit': return { kind: 'editing', draft: event.draft };
    case 'begin_delete': return { kind: 'confirming_delete', plan: null };
    case 'submit_edit': return {
      kind: 'submitting_edit',
      draft: event.draft,
      patch: event.patch,
      plan: event.plan,
      forced: event.plan !== null,
      surface: event.surface,
    };
    case 'submit_delete': return {
      kind: 'submitting_delete',
      plan: event.plan,
      forced: event.plan !== null,
    };
    case 'guard_edit': return {
      kind: 'confirming_edit',
      draft: event.draft,
      patch: event.patch,
      plan: event.plan,
    };
    case 'guard_delete': return { kind: 'confirming_delete', plan: event.plan };
    case 'fail_edit': return {
      kind: 'edit_failed',
      draft: event.draft,
      patch: event.patch,
      plan: event.plan,
      forced: event.forced,
      retryRead: event.retryRead,
      before: event.before,
    };
    case 'fail_delete': return {
      kind: 'delete_failed',
      plan: event.plan,
      forced: event.forced,
      retryRead: event.retryRead,
      before: event.before,
    };
    case 'commit_impact': return event.action === 'edit'
      ? { kind: 'committed_edit_impact', plan: event.plan, complete: event.complete, landingFailed: false }
      : { kind: 'committed_delete_impact', plan: event.plan, complete: event.complete, landingFailed: false };
    case 'settled': return idleManageStage();
    case 'cancel': return applyManageStageTransition(MANAGE_STAGE_CANCEL, stage, event);
    case 'retry': return applyManageStageTransition(MANAGE_STAGE_RETRY, stage, event);
    case 'edit_draft': return applyManageStageTransition(MANAGE_STAGE_EDIT_DRAFT, stage, event);
    case 'dismiss_unresolved': return applyManageStageTransition(MANAGE_STAGE_DISMISS_UNRESOLVED, stage, event);
    case 'land': return applyManageStageTransition(MANAGE_STAGE_LANDING, stage, event);
  }
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
  | 'baseUrlCredential';

export const SOURCE_EDIT_REASON_KEY: Record<SourceEditInvalidReason, string> = {
  displayNameRequired: 'settings.models.sourceDetail.edit.validation.displayNameRequired',
  displayNameTooLong: 'settings.models.sourceDetail.edit.validation.displayNameTooLong',
  displayNameCredential: 'settings.models.sourceDetail.edit.validation.displayNameCredential',
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
    return { valid: true, value };
  }

  for (const key of parsed.searchParams.keys()) {
    const normalized = key.trim().toLowerCase().replace(/[-.]/g, '_');
    if (
      CREDENTIAL_QUERY_KEYS.has(normalized)
      || CREDENTIAL_QUERY_MARKERS.some((marker) => normalized.includes(marker))
    ) return { valid: false, reason: 'baseUrlCredential' };
  }

  // The browser owns only the warning that prevents a user from persisting a
  // secret. URL acceptance and normalization belong entirely to the server.
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
