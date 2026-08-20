import type {
  MemoryEndpointConfig,
  MemoryEndpointPatch,
  MemoryRerankProvider,
  MemorySettingsResult,
} from '../context/ApiContext';
import { isMemoryOk } from './memoryRead';


export const MEMORY_RERANK_PROVIDERS = ['deepinfra', 'vllm', 'dashscope'] as const;
export const DEFAULT_MEMORY_RERANK_PROVIDER: MemoryRerankProvider = 'deepinfra';
export const DASHSCOPE_RERANK_MODEL = 'gte-rerank-v2';

export type EndpointDraft = {
  baseUrl: string;
  model: string;
  apiKey: string;
  clearKey: boolean;
  provider?: MemoryRerankProvider;
};

export const normalizeRerankProvider = (
  value: string | null | undefined,
): MemoryRerankProvider => (
  MEMORY_RERANK_PROVIDERS.includes(value as MemoryRerankProvider)
    ? value as MemoryRerankProvider
    : DEFAULT_MEMORY_RERANK_PROVIDER
);
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

export const draftFromConfig = (config: MemoryEndpointConfig): EndpointDraft => ({
  baseUrl: config.base_url ?? '',
  model: config.model ?? '',
  apiKey: '',
  clearKey: false,
  provider: normalizeRerankProvider(config.provider),
});

// `allowClear` gates the explicit `api_key: null` clear. `identityLocked` protects
// an existing embedding vector space while still allowing credential rotation.
export function buildEndpointPatch(
  draft: EndpointDraft,
  original: MemoryEndpointConfig,
  allowClear: boolean,
  identityLocked = false,
  clearEndpoint = false,
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
    if (draft.provider !== undefined) {
      const provider = normalizeRerankProvider(draft.provider);
      const originalProvider = original.provider ?? null;
      // A new optional endpoint has no saved provider. Always send the selected
      // one so preflight cannot fall back to DeepInfra's `/{model}` path.
      if (originalProvider == null || provider !== normalizeRerankProvider(originalProvider)) {
        patch.provider = provider;
        changed = true;
      }
    }
  }
  const trimmedKey = draft.apiKey.trim();
  if (trimmedKey) {
    patch.api_key = trimmedKey;
    changed = true;
  } else if (draft.clearKey && allowClear) {
    patch.api_key = null;
    if (clearEndpoint) {
      patch.base_url = null;
      patch.model = null;
      if (original.provider != null) {
        patch.provider = null;
      }
    }
    changed = true;
  }
  return changed ? patch : undefined;
}
