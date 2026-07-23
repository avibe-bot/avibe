import type { MemoryEndpointConfig, MemoryEndpointPatch } from '../context/ApiContext';


export type EndpointDraft = { baseUrl: string; model: string; apiKey: string; clearKey: boolean };

export const draftFromConfig = (config: MemoryEndpointConfig): EndpointDraft => ({
  baseUrl: config.base_url ?? '',
  model: config.model ?? '',
  apiKey: '',
  clearKey: false,
});

// `allowClear` gates the explicit `api_key: null` clear. `identityLocked` protects
// an existing embedding vector space while still allowing credential rotation.
export function buildEndpointPatch(
  draft: EndpointDraft,
  original: MemoryEndpointConfig,
  allowClear: boolean,
  identityLocked = false,
): MemoryEndpointPatch | undefined {
  const patch: MemoryEndpointPatch = {};
  let changed = false;
  if (!identityLocked) {
    const baseUrl = draft.baseUrl.trim() || null;
    if (baseUrl !== (original.base_url ?? null)) {
      patch.base_url = baseUrl;
      changed = true;
    }
    const model = draft.model.trim() || null;
    if (model !== (original.model ?? null)) {
      patch.model = model;
      changed = true;
    }
  }
  const trimmedKey = draft.apiKey.trim();
  if (trimmedKey) {
    patch.api_key = trimmedKey;
    changed = true;
  } else if (draft.clearKey && allowClear) {
    patch.api_key = null;
    changed = true;
  }
  return changed ? patch : undefined;
}
