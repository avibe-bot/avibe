// One place that turns a provider *id* into the two things a surface needs to
// present it: a brand label a person recognises, and the catalog vendor id its
// artwork is filed under.
//
// It exists because the same provider arrives under different ids depending on
// who is talking. The shipped API-key catalog files Gemini under `gemini`; an
// OpenCode config calls the same provider `google`, Kimi `moonshot` and Qwen
// `alibaba-cn`. A surface that renders those ids raw shows an internal name, and
// one that maps them ad hoc grows a second, drifting table. So the alias lives
// here once, and both the setup provider picker and the import rows read it.
//
// The mapping only ever *adds* recognition: an id with no alias and no catalog
// row keeps its own slug, which is what a self-hosted or relay provider should
// show. Nothing here infers identity from a backend name.
import {
  apiKeyVendorPreset,
  API_KEY_VENDOR_PRESETS,
} from '../models/apiKeyVendors';

/** OpenCode/native provider id → the id the shipped vendor catalog files the
 *  same brand under. Only ids that genuinely name the same provider belong
 *  here; ids that already match the catalog need no row. */
export const PROVIDER_VENDOR_ALIAS: Readonly<Record<string, string>> = {
  google: 'gemini',
  'google-vertex': 'gemini',
  moonshot: 'kimi',
  moonshotai: 'kimi',
  'alibaba-cn': 'qwen',
  alibaba: 'qwen',
  dashscope: 'qwen',
  zhipu: 'zhipuai',
};

/** The catalog vendor id a provider id resolves to — itself when it is already
 *  a catalog id or has no known alias. */
export function providerVendorId(providerId: string): string {
  const id = providerId.trim().toLowerCase();
  return PROVIDER_VENDOR_ALIAS[id] ?? id;
}

/**
 * What to call a provider.
 *
 * `serverName` wins when the server already sent something friendlier than the
 * id — the OpenCode catalog does this ("Alibaba (China)"), and Claude/Codex
 * migration rows carry real brand names ("Anthropic", "ChatGPT"). When the
 * server only echoed the id back (every OpenCode migration row does), the
 * shipped catalog supplies the brand label instead. An id nothing recognises
 * keeps its slug rather than borrowing a neighbour's name.
 */
export function providerLabel(providerId: string, serverName?: string | null): string {
  const id = providerId.trim();
  const name = serverName?.trim();
  if (name && name.toLowerCase() !== id.toLowerCase()) return name;
  return apiKeyVendorPreset(providerVendorId(id))?.label ?? name ?? id;
}

/**
 * What to call a provider that is standing in a *named brand slot*.
 *
 * The setup shortlist offers eight brands in an approved order, so those rows
 * carry the brand's own name and mark: an OpenCode catalog row that calls Qwen
 * "Alibaba (China)" is the right label for a variant listed under More and the
 * wrong one for the Qwen slot. Everywhere there is no slot — More, search, the
 * chosen-provider capsule — `providerLabel` stays correct, because there the
 * job is to tell two rows of the same brand apart rather than to name the
 * brand. A provider the catalog does not know has no brand slot to fill, so it
 * falls back to the same label it would show anywhere else.
 */
export function providerBrandLabel(providerId: string, serverName?: string | null): string {
  return apiKeyVendorPreset(providerVendorId(providerId))?.label ?? providerLabel(providerId, serverName);
}

/**
 * The API-key providers the setup picker offers before More.
 *
 * Declared here rather than taken as "the catalog's first eight": the catalog is
 * ranked for the Add-API-key dropdown, and setup's shortlist is its own product
 * decision. `providerIdentity.test.ts` holds every entry to a real catalog row,
 * so the two cannot drift apart silently.
 */
export const SETUP_PRIMARY_VENDORS: readonly string[] = [
  'openai',
  'anthropic',
  'xai',
  'gemini',
  'deepseek',
  'qwen',
  'kimi',
  'openrouter',
];

const PRIMARY_RANK = new Map(SETUP_PRIMARY_VENDORS.map((id, index) => [id, index]));

/** Rank within the primary shortlist, or `null` for a provider that belongs
 *  under More. Resolves through the alias, so an OpenCode `google` row ranks
 *  where Gemini does. */
export function setupPrimaryRank(providerId: string): number | null {
  return PRIMARY_RANK.get(providerVendorId(providerId)) ?? null;
}

/** Every catalog id, for tests and for callers that need the full vendor set. */
export const CATALOG_VENDOR_IDS: readonly string[] = API_KEY_VENDOR_PRESETS.map((preset) => preset.id);
