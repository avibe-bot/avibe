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

export type HubSupplyBlock = 'hub_disabled' | 'gateway_off';

// Backends left with no model to run on by the Model Hub's state, and why. The
// server decides (``hub_supply_block``) and GET /api/config carries the answer.
export const hubSupplyBlocks = (config: unknown): ReadonlyMap<string, HubSupplyBlock> => {
  const blocks = (config as { agent_supply_blocks?: unknown } | null)?.agent_supply_blocks;
  if (!blocks || typeof blocks !== 'object') return new Map();
  return new Map(
    Object.entries(blocks).filter((entry): entry is [string, HubSupplyBlock] =>
      entry[1] === 'hub_disabled' || entry[1] === 'gateway_off'),
  );
};
