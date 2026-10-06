import { AGENT_BACKENDS } from '@/lib/agentBackends';

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

export type HubSupplyBlock = 'gatewayOff' | 'hubDisabled';

// Backends left with no model to run on by the Model Hub's state, and why. A
// backend without its own CLI gets every model through the Hub, so the Hub being
// disabled on this instance leaves it none; a Hub-supplied backend has none while
// the user has turned the gateway off (Models → Start/Stop).
export const hubSupplyBlocks = (config: unknown): ReadonlyMap<string, HubSupplyBlock> => {
  if (!modelHubEnabledFromConfig(config)) {
    return new Map(
      AGENT_BACKENDS.filter((backend) => !backend.capabilities.supports_cli).map((backend) => [backend.id, 'hubDisabled']),
    );
  }
  const hub = (config as { model_hub?: { enabled?: unknown; agents?: Record<string, { mode?: unknown } | null> } })
    .model_hub;
  if (!hub || hub.enabled !== false) return new Map();
  return new Map(
    Object.entries(hub.agents ?? {})
      .filter(([, agent]) => agent?.mode === 'hub')
      .map(([backend]) => [backend, 'gatewayOff']),
  );
};
