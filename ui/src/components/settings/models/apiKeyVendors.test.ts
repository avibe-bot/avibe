import { describe, expect, it } from 'vitest';

import { API_KEY_VENDOR_PRESETS, apiKeyVendorPreset, CUSTOM_VENDOR } from './apiKeyVendors';
import { SOURCE_PROTOCOLS } from './types';

// The dialog casts the shipped JSON to its preset type on the strength of the
// Python loader's validation. These hold the file to the same properties from
// this side, so a row that would render a broken dropdown — an unpinnable
// protocol, a blank label, a second `custom` — fails here instead of at the
// moment a user picks it. Stated as properties over every row, never as a list
// of the vendors that happen to ship today.
describe('the shipped API-key vendor catalog', () => {
  it('offers something to pick', () => {
    expect(API_KEY_VENDOR_PRESETS.length).toBeGreaterThan(0);
  });

  it('pins every row to an interface this UI can name', () => {
    for (const preset of API_KEY_VENDOR_PRESETS) {
      expect(SOURCE_PROTOCOLS).toContain(preset.protocol);
    }
  });

  it('gives every row an id, a label, and an absolute official address', () => {
    for (const preset of API_KEY_VENDOR_PRESETS) {
      expect(preset.id.trim()).not.toBe('');
      expect(preset.label.trim()).not.toBe('');
      expect(preset.official_base_url).toMatch(/^https?:\/\/\S+$/);
    }
  });

  // The Python reader normalizes as it loads — `normalize_model_hub_vendor_id`
  // lowercases and trims the id, `label.strip()` the label. This reader does
  // not: it hands the raw JSON to the dropdown and sends the raw id. That is
  // only equivalent while the shipped file is ALREADY in normal form, so the
  // equivalence is asserted here rather than assumed. A row that needed
  // normalizing would otherwise put a differently-spelled id on the wire than
  // the one the server stores.
  it('ships every row already in the form the Python reader would normalize it to', () => {
    for (const preset of API_KEY_VENDOR_PRESETS) {
      expect(preset.id).toBe(preset.id.trim().toLowerCase());
      // The persisted vendor-id grammar, from `normalize_model_hub_vendor_id`.
      expect(preset.id).toMatch(/^[a-z0-9]+(?:[._-][a-z0-9]+)*$/);
      expect(preset.label).toBe(preset.label.trim());
      expect(preset.official_base_url).toBe(preset.official_base_url.trim());
    }
  });

  it('names each vendor once, and never names the one option that is not a vendor', () => {
    const ids = API_KEY_VENDOR_PRESETS.map((preset) => preset.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).not.toContain(CUSTOM_VENDOR);
  });

  it('resolves a row by its id, and anything else to no preset at all', () => {
    for (const preset of API_KEY_VENDOR_PRESETS) {
      expect(apiKeyVendorPreset(preset.id)).toBe(preset);
    }
    // Both the custom option and an id a later catalog dropped land here: with no
    // preset there is no pin, which is the rung that asks rather than the rung
    // that would assert an interface nothing defines.
    expect(apiKeyVendorPreset(CUSTOM_VENDOR)).toBeNull();
    expect(apiKeyVendorPreset('a-vendor-this-catalog-does-not-carry')).toBeNull();
  });
});
