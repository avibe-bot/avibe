import type { Source, SourcePatch } from './types';
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

/** Every persisted Source owns its display name and may be removed. */
export const canEditSource = (_source: Source): boolean => true;
export const canDeleteSource = (_source: Source): boolean => true;

/** Only Avibe-held API-key sources own an endpoint that this editor can change. */
export const canEditSourceEndpoint = (source: Source): boolean =>
  source.kind === 'api_key' && source.supply_channel === 'hub' && Boolean(source.credential_ref);

export const MANAGE_CAPABILITY: Record<ManageKind, (source: Source) => boolean> = {
  edit_source: canEditSource,
  delete_source: canDeleteSource,
};

export const manageActions = (source: Source): ManageKind[] =>
  (Object.keys(MANAGE_CAPABILITY) as ManageKind[]).filter((kind) => MANAGE_CAPABILITY[kind](source));

export type SourceEditDraft = {
  displayName: string;
  baseUrl: string;
};

export type SourceEditAssessment = {
  valid: boolean;
  patch: SourcePatch | null;
};

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

const normalizedBaseUrl = (draft: string): { valid: boolean; value: string | null } => {
  const value = draft.trim();
  if (!value) return { valid: true, value: null };

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return { valid: false, value: null };
  }
  if (
    !['http:', 'https:'].includes(parsed.protocol.toLowerCase())
    || !parsed.hostname
    || parsed.username
    || parsed.password
    || parsed.hash
    || containsCredentialMaterial(value)
  ) return { valid: false, value: null };

  for (const key of parsed.searchParams.keys()) {
    const normalized = key.trim().toLowerCase().replace(/[-.]/g, '_');
    if (
      CREDENTIAL_QUERY_KEYS.has(normalized)
      || CREDENTIAL_QUERY_MARKERS.some((marker) => normalized.includes(marker))
    ) return { valid: false, value: null };
  }

  const path = parsed.pathname.replace(/\/+$/, '');
  return {
    valid: true,
    value: `${parsed.protocol.toLowerCase()}//${parsed.host}${path}${parsed.search}`,
  };
};

/** Validate, normalize and diff the source editor without mutating its held Source. */
export const assessSourceEdit = (source: Source, draft: SourceEditDraft): SourceEditAssessment => {
  const displayName = draft.displayName.trim();
  if (
    !displayName
    || !optionalTrimmedTextWithin(draft.displayName, SOURCE_DISPLAY_NAME_MAX_LENGTH)
    || containsCredentialMaterial(displayName)
  ) return { valid: false, patch: null };

  const patch: SourcePatch = {};
  if (displayName !== source.display_name) patch.display_name = displayName;

  if (canEditSourceEndpoint(source)) {
    const endpoint = normalizedBaseUrl(draft.baseUrl);
    if (!endpoint.valid) return { valid: false, patch: null };
    if (endpoint.value !== (source.base_url ?? null)) patch.base_url = endpoint.value;
  }

  return { valid: true, patch: Object.keys(patch).length > 0 ? patch : null };
};
