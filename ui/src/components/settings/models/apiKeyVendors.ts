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
};

/** The dropdown's default, and the ladder's rungs 2 and 3: no preset, no pin.
 *  It is the one vendor id the catalog may never contain. */
export const CUSTOM_VENDOR = 'custom';

/** Catalog order is file order. The backend ships the list already ranked, and
 *  re-sorting it here would put the ranking in a second place. */
export const API_KEY_VENDOR_PRESETS = catalog as readonly ApiKeyVendorPreset[];

/** The preset a vendor id names, or `null` for 自定义 — which is also what an id
 *  the shipped catalog no longer carries resolves to, so a row dropped upstream
 *  degrades to the custom path rather than pinning a protocol nothing defines. */
export const apiKeyVendorPreset = (vendor: string): ApiKeyVendorPreset | null =>
  API_KEY_VENDOR_PRESETS.find((preset) => preset.id === vendor) ?? null;
