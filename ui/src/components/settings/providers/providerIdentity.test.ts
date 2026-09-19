// The promise `providerIdentity.ts` makes in its own doc comment: every alias
// target and every shortlist entry names a real row in the shipped catalog, so
// the table and the catalog cannot drift apart silently. A dropped catalog row
// would otherwise surface as a provider rendering its internal slug — or, for
// the shortlist, as a setup picker with a hole in it.
import { describe, expect, it } from 'vitest';

import { apiKeyVendorPreset } from '../models/apiKeyVendors';
import {
  CATALOG_VENDOR_IDS,
  PROVIDER_VENDOR_ALIAS,
  SETUP_PRIMARY_VENDORS,
  providerBrandLabel,
  providerLabel,
  providerVendorId,
  setupPrimaryRank,
} from './providerIdentity';

describe('the alias table', () => {
  it('resolves every alias to a catalog row that exists', () => {
    for (const target of Object.values(PROVIDER_VENDOR_ALIAS)) {
      expect(CATALOG_VENDOR_IDS, `alias target ${target}`).toContain(target);
    }
  });

  it('never aliases an id the catalog already owns', () => {
    // An id that is itself a catalog row must resolve to itself; aliasing one
    // would silently redirect a provider onto a neighbour's brand.
    for (const source of Object.keys(PROVIDER_VENDOR_ALIAS)) {
      expect(CATALOG_VENDOR_IDS, `alias source ${source}`).not.toContain(source);
    }
  });

  it('maps the ids OpenCode actually uses onto their catalog brand', () => {
    expect(providerVendorId('google')).toBe('gemini');
    expect(providerVendorId('moonshotai')).toBe('kimi');
    expect(providerVendorId('alibaba-cn')).toBe('qwen');
    expect(providerVendorId('zhipu')).toBe('zhipuai');
  });

  it('leaves an unrecognised provider as itself', () => {
    // A self-hosted or relay provider should show its own slug, not borrow one.
    expect(providerVendorId('my-internal-relay')).toBe('my-internal-relay');
  });

  it('reads ids case- and whitespace-insensitively', () => {
    expect(providerVendorId('  Google ')).toBe('gemini');
  });
});

describe('providerLabel', () => {
  it('keeps a server label that says more than the id', () => {
    expect(providerLabel('alibaba-cn', 'Alibaba (China)')).toBe('Alibaba (China)');
  });

  it('keeps a non-ASCII server label untouched', () => {
    expect(providerLabel('zhipu', '智谱 AI')).toBe('智谱 AI');
  });

  it('falls back to the catalog brand when the server only echoed the id', () => {
    // Every OpenCode migration row does exactly this, which is why the row
    // cannot be trusted to be friendly on its own.
    const label = providerLabel('moonshot', 'moonshot');
    expect(label).toBe(apiKeyVendorPreset('kimi')?.label);
    expect(label).not.toBe('moonshot');
  });

  it('falls back to the catalog brand when the server sent nothing', () => {
    expect(providerLabel('google')).toBe(apiKeyVendorPreset('gemini')?.label);
  });

  it('keeps the slug when neither the catalog nor the server knows the name', () => {
    expect(providerLabel('my-internal-relay')).toBe('my-internal-relay');
    expect(providerLabel('my-internal-relay', 'my-internal-relay')).toBe('my-internal-relay');
  });
});

describe('providerBrandLabel', () => {
  it('names the brand, not the variant, when a row stands in a brand slot', () => {
    // The same row reads two ways on purpose: "Qwen" in the slot it fills,
    // "Alibaba (China)" under More where its job is to be told apart from the
    // other Qwen rows.
    expect(providerBrandLabel('alibaba-cn', 'Alibaba (China)')).toBe(apiKeyVendorPreset('qwen')?.label);
    expect(providerLabel('alibaba-cn', 'Alibaba (China)')).toBe('Alibaba (China)');
  });

  it('agrees with providerLabel when the server added nothing', () => {
    expect(providerBrandLabel('google')).toBe(providerLabel('google'));
    expect(providerBrandLabel('moonshot', 'moonshot')).toBe(providerLabel('moonshot', 'moonshot'));
  });

  it('leaves a provider with no brand slot exactly as it was', () => {
    expect(providerBrandLabel('my-internal-relay', '内部中转')).toBe('内部中转');
    expect(providerBrandLabel('my-internal-relay')).toBe('my-internal-relay');
  });
});

describe('the setup shortlist', () => {
  it('names only providers the shipped catalog can render', () => {
    for (const vendor of SETUP_PRIMARY_VENDORS) {
      expect(apiKeyVendorPreset(vendor), `shortlist entry ${vendor}`).not.toBeNull();
    }
  });

  it('lists each provider once', () => {
    expect(new Set(SETUP_PRIMARY_VENDORS).size).toBe(SETUP_PRIMARY_VENDORS.length);
  });

  it('ranks in declared order, which is the order setup renders', () => {
    const ranks = SETUP_PRIMARY_VENDORS.map((id) => setupPrimaryRank(id));
    expect(ranks).toEqual(SETUP_PRIMARY_VENDORS.map((_, index) => index));
  });

  it('ranks an aliased id where its brand ranks', () => {
    // An OpenCode `google` row belongs next to Gemini, not under More.
    expect(setupPrimaryRank('google')).toBe(setupPrimaryRank('gemini'));
    expect(setupPrimaryRank('alibaba-cn')).toBe(setupPrimaryRank('qwen'));
  });

  it('puts everything else under More rather than inventing a rank', () => {
    expect(setupPrimaryRank('my-internal-relay')).toBeNull();
  });
});
