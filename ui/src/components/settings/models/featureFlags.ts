// Model Hub UI capability projection. Availability is owned by the backend and
// arrives in GET /api/config; the browser has no independent release switch.

export const modelHubEnabledFromConfig = (config: unknown): boolean => {
  if (!config || typeof config !== 'object') return false;
  const capabilities = (config as { capabilities?: unknown }).capabilities;
  if (!capabilities || typeof capabilities !== 'object') return false;
  const modelHub = (capabilities as { model_hub?: unknown }).model_hub;
  return Boolean(
    modelHub &&
      typeof modelHub === 'object' &&
      (modelHub as { enabled?: unknown }).enabled === true,
  );
};

/**
 * 'mock' serves the recorded server corpus; 'live' calls `/api/models/*`.
 * Components consume one injected client surface and never branch on this mode.
 * Flip to 'mock' only for hermetic UI runs without a backend.
 */
export const MODELS_API_MODE = 'live' as 'mock' | 'live';

// Keep the literal and import in one compilation unit: Vite removes this branch
// (and the corpus chunk) from live builds, while flipping the existing flag is
// sufficient to make the same product route load the replay client.
export const loadMockModelsApiForMode = MODELS_API_MODE === 'mock'
  ? () => import('./mock-only/modelsApi.mockEntry')
      .then(({ loadMockModelsApi }) => loadMockModelsApi())
  : null;
