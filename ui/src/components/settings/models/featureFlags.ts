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
 * 'mock' serves typed fixtures from `mockData.ts`; 'live' calls the real
 * `/api/models/*` endpoints (L2). Now 'live': all backend lanes are merged, so
 * shipping the nav must serve real data — never fabricated mock sources. (Flip
 * to 'mock' only for hermetic pixel/screenshot runs with no backend.)
 */
export const MODELS_API_MODE: 'mock' | 'live' = 'live';
