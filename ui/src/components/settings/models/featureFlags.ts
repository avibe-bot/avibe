// Model Hub UI — feature flags.
//
// This lane (L4) lands ahead of its backend dependencies (L2 REST API, L3
// backend injection, L5 model-menu drawers). The flags below keep the surface
// reviewable and pixel-checkable now while preventing fabricated data from
// reaching end users until the real endpoints exist.
//
// Flip sequence when dependencies merge (orchestrator):
//   1. L2 API merged      → set MODELS_API_MODE = 'live'
//   2. L2/L3 both merged   → set MODEL_HUB_NAV_ENABLED = true (advertise nav)
//   3. L5 menus merged     → set MODEL_MENUS_ENABLED = true (wire 模型菜单)

/**
 * Advertises the 设置 → 模型 entry in the admin sidebar. OFF by default so we
 * do not surface mock-backed data as a first-class destination. The route is
 * always registered (reachable by direct URL) for review + pixel verification.
 */
export const MODEL_HUB_NAV_ENABLED = false;

/**
 * 'mock' serves typed fixtures from `mockData.ts`; 'live' calls the real
 * `/api/models/*` endpoints (L2). The client module switches on this value.
 */
export const MODELS_API_MODE: 'mock' | 'live' = 'mock';

/**
 * Wires the 模型菜单 buttons on the Agent card to L5's mapping / menu drawers.
 * OFF until L5 lands — the buttons stay visible (pixel fidelity) but explain
 * that the menus are coming rather than opening a non-existent drawer.
 */
export const MODEL_MENUS_ENABLED = false;

/**
 * Offers the consent-gated hub-held subscription option (`subscription_hub_
 * experimental`, spec §4.1/§7) inside the connect-subscription dialog. OFF by
 * default: subscriptions connect via the sanctioned native_cli channel only.
 * When ON, choosing "hub" for a subscription requires the ban-risk consent
 * dialog (copy from S2 §9) and marks the resulting source 实验.
 */
export const SUBSCRIPTION_HUB_EXPERIMENTAL = false;
