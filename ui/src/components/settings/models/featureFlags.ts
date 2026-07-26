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

/**
 * Wires the 模型菜单 buttons on the Agent card to L5's mapping / menu drawers.
 * ON now that L5 is merged.
 */
export const MODEL_MENUS_ENABLED = true;

/**
 * Offers the consent-gated hub-held subscription option (`subscription_hub_
 * experimental`, spec §4.1/§7) inside the connect-subscription dialog. OFF by
 * default: subscriptions connect via the sanctioned native_cli channel only.
 * When ON, choosing "hub" for a subscription requires the ban-risk consent
 * dialog (copy from S2 §9) and marks the resulting source 实验.
 */
export const SUBSCRIPTION_HUB_EXPERIMENTAL = false;
