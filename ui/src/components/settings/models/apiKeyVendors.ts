// The Web UI half of the shipped API-key vendor catalog. This reads the SAME
// tracked file as ``vibe/model_hub_runtime/api_key_vendors.py`` — there is
// deliberately no TypeScript copy of the ids, labels, official base URLs or
// pinned protocols. A second copy is exactly how a preset's URL drifts from the
// one the server pins its protocol by, and the drift would surface as a
// `discovery_failed` the user cannot explain. Vite inlines the JSON at build
// time, so the browser bundle carries the values with no runtime coupling to the
// Python package — the same arrangement as ``src/lib/messageTypes.ts`` and
// ``vibe/message_types.json``.
//
// The Python loader is the validating authority: it refuses a catalog carrying a
// `custom` id, a duplicate id, a blank label, or an unsupported protocol, and
// the service does not start until the file is valid. The assertion below trusts
// that check rather than restating it in a second place; `apiKeyVendors.test.ts`
// holds the shipped file to it from this side too, so a bad row fails a test
// instead of rendering a dropdown that pins a protocol nothing defines.
import catalog from '../../../../../vibe/data/api_key_vendors.json';
import type { SourceProtocol } from './types';

export type ApiKeyVendorPreset = {
  id: string;
  label: string;
  official_base_url: string;
  protocol: SourceProtocol;
  /** The name mainland users know the vendor by, shown before `label` in Chinese. */
  label_zh?: string;
  /** `cn` for a mainland-China vendor, which the Chinese UI lists earlier. */
  region?: 'cn';
  built_in?: boolean;
  source_id?: string;
};

/** The dropdown's default, and the ladder's rungs 2 and 3: no preset, no pin.
 *  It is the one vendor id the catalog may never contain. */
export const CUSTOM_VENDOR = 'custom';

/** Catalog order is file order. The backend ships the list already ranked, and
 *  re-sorting it here would put the ranking in a second place. */
export const ALL_API_KEY_VENDOR_PRESETS = catalog as readonly ApiKeyVendorPreset[];

/** Tool-only search rows belong to the web-search card, not the generic API-key
 * model form. Keep the generic picker dormant until that card sends the
 * tool-only request shape. */
export const API_KEY_VENDOR_PRESETS = ALL_API_KEY_VENDOR_PRESETS.filter(
  (preset) => preset.protocol !== 'search',
);

/** The full catalog lookup is also used for labels on already persisted rows. */
export const apiKeyVendorPreset = (vendor: string): ApiKeyVendorPreset | null =>
  ALL_API_KEY_VENDOR_PRESETS.find((preset) => preset.id === vendor) ?? null;

/** Resolve only a model-capable row for the generic API-key form. */
export const modelApiKeyVendorPreset = (vendor: string): ApiKeyVendorPreset | null =>
  API_KEY_VENDOR_PRESETS.find((preset) => preset.id === vendor) ?? null;

/** First in every language: the two flagships most people arrive to add. */
const FLAGSHIP_VENDORS: readonly string[] = ['openai', 'anthropic'];

/**
 * The 供应商 picker's rows for a UI language.
 *
 * English reads the catalog's own order. Chinese keeps the flagships first, then
 * the mainland vendors, then everyone else, each group in catalog order; a
 * mainland vendor carries its Chinese name beside the English one, which is
 * also what search matches. The ranking stays in the catalog file: this only
 * regroups it, and adding a vendor is still one row there.
 */
export function localizedApiKeyVendorOptions(language: string): { value: string; label: string }[] {
  const chinese = language.toLowerCase().startsWith('zh');
  const group = (preset: ApiKeyVendorPreset) => (
    !chinese ? 0 : FLAGSHIP_VENDORS.includes(preset.id) ? 0 : preset.region === 'cn' ? 1 : 2
  );
  return API_KEY_VENDOR_PRESETS
    .map((preset, index) => ({ preset, index }))
    .sort((a, b) => group(a.preset) - group(b.preset) || a.index - b.index)
    .map(({ preset }) => ({
      value: preset.id,
      label: chinese && preset.label_zh ? `${preset.label_zh} ${preset.label}` : preset.label,
    }));
}
