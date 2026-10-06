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

// Backends whose supply runs through the Model Hub gateway while the user has
// turned the gateway off (Models → Start/Stop): they have no model to run on.
export const backendsWithGatewayOff = (config: unknown): ReadonlySet<string> => {
  if (!modelHubEnabledFromConfig(config)) return new Set();
  const hub = (config as { model_hub?: { enabled?: unknown; agents?: Record<string, { mode?: unknown } | null> } })
    .model_hub;
  if (!hub || hub.enabled !== false) return new Set();
  return new Set(
    Object.entries(hub.agents ?? {})
      .filter(([, agent]) => agent?.mode === 'hub')
      .map(([backend]) => backend),
  );
};
