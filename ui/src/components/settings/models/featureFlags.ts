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

const HUB_SUPPLY_BLOCKS = ['hub_disabled', 'engine_unsupported', 'gateway_off'] as const;
export type HubSupplyBlock = typeof HUB_SUPPLY_BLOCKS[number];

// Backends left with no model to run on by the Model Hub's state, and why. The
// server decides (``hub_supply_block``) and GET /api/config carries the answer.
export const hubSupplyBlocks = (config: unknown): ReadonlyMap<string, HubSupplyBlock> => {
  const blocks = (config as { agent_supply_blocks?: unknown } | null)?.agent_supply_blocks;
  if (!blocks || typeof blocks !== 'object') return new Map();
  return new Map(
    Object.entries(blocks).filter((entry): entry is [string, HubSupplyBlock] =>
      (HUB_SUPPLY_BLOCKS as readonly unknown[]).includes(entry[1])),
  );
};
