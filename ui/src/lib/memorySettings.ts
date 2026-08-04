import type {
  MemoryEndpointConfig,
  MemoryEndpointPatch,
  MemorySettings,
  MemorySettingsResult,
} from '../context/ApiContext';
import { isMemoryOk } from './memoryRead';


export type EndpointDraft = { baseUrl: string; model: string; apiKey: string; clearKey: boolean };
export type MemorySetupStage = 'loading' | 'runtime-required' | 'setup' | 'manage';

export function memorySetupStage(runtimeInstalled: boolean | null, enabled: boolean | null): MemorySetupStage {
  if (runtimeInstalled === false) return 'runtime-required';
  if (runtimeInstalled === null || enabled === null) return 'loading';
  return enabled ? 'manage' : 'setup';
}

export const memoryRuntimeRecoveryAvailable = (
  runtimeInstalled: boolean | null,
  settingsLoaded: boolean,
): boolean => runtimeInstalled === false && settingsLoaded;

export const memoryNavShouldBeVisible = (settings: MemorySettingsResult): boolean =>
  isMemoryOk(settings) && settings.enabled;

export const memoryDiagnostics = (settings: Pick<MemorySettings, 'diagnostics'>) => ({
  logProviderCalls: settings.diagnostics?.log_provider_calls === true,
  // Missing authority metadata must fail closed during a rolling upgrade.
  mutable: settings.diagnostics?.mutable === true,
});

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
