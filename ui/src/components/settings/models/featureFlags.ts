// Model Hub UI — feature flags.
//
// All feature lanes are merged (L2 REST API #963, L3 injection #976, L4 page
// #966, L5 menus #977) and the integration seams are closed (agent-supply v1.2
// builtin_models / standard_vendors, so the UI no longer hardcodes any menu or
// vendor list). The documented flip sequence below is therefore complete — the
// surfaces ship ON against the real endpoints (integration LI, 2026-07-24):
//   1. L2 API merged       → MODELS_API_MODE = 'live'          ✓ (done here)
//   2. L2/L3 both merged    → MODEL_HUB_NAV_ENABLED = true      ✓ (done here)
//   3. L5 menus merged      → MODEL_MENUS_ENABLED = true        ✓ (done here)
// subscription_hub_experimental stays OFF — it is a consent-gated behavior, not
// a UI-readiness flag. Live end-to-end verification is the post-merge Incus pass.

/**
 * Advertises the 设置 → 模型 entry in the admin sidebar. ON now that the surface
 * is backed by the real endpoints; the route is also reachable by direct URL.
 */
export const MODEL_HUB_NAV_ENABLED = true;

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
