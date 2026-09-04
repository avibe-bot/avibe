// @vitest-environment jsdom
// A mark is only worth drawing if it tells one vendor from another, so the
// properties held here are drawn-at-all and drawn-once — asserted over whatever
// the shipped catalog contains rather than over a list restated in the test,
// which would keep passing for the twelve rows it knew about.
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { API_KEY_VENDOR_PRESETS, CUSTOM_VENDOR } from './apiKeyVendors';
import { VendorGlyph } from './vendorGlyph';

afterEach(cleanup);

/** Every choice the 服务商 field offers, including the one that is not a vendor. */
const CHOICES = [CUSTOM_VENDOR, ...API_KEY_VENDOR_PRESETS.map((preset) => preset.id)];

const glyphOf = (vendor: string) => {
  const { container, unmount } = render(<VendorGlyph vendor={vendor} />);
  const svg = container.querySelector('svg');
  const drawn = { className: svg?.getAttribute('class') ?? '', hidden: svg?.getAttribute('aria-hidden'), art: svg?.innerHTML ?? '', markup: svg?.outerHTML ?? '' };
  unmount();
  return drawn;
};

describe('VendorGlyph', () => {
  it('draws every choice as one theme-following mark of the shared size', () => {
    expect(CHOICES.length).toBeGreaterThan(1);
    for (const vendor of CHOICES) {
      const glyph = glyphOf(vendor);
      expect(glyph.art, `${vendor} draws nothing`).not.toBe('');
      expect(glyph.className, `${vendor} is not sized with the others`)
        .toContain('model-hub-add-key-vendor-glyph');
      // A mark names the row next to it; it is not a second thing to read.
      expect(glyph.hidden, `${vendor} is exposed to a screen reader`).toBe('true');
      // Theme-following means the ink comes from the text around it. A baked
      // channel would be a light-theme defect nobody sees while developing dark.
      expect(glyph.markup, `${vendor} bakes its own color`).not.toMatch(/#[\da-f]{3,8}\b|\b(?:rgba?|hsla?|oklch)\(/i);
    }
  });

  it('never draws one mark for two choices', () => {
    const owner = new Map<string, string>();
    for (const vendor of CHOICES) {
      const { art } = glyphOf(vendor);
      expect(owner.get(art), `${vendor} draws the same mark as ${owner.get(art)}`).toBeUndefined();
      owner.set(art, vendor);
    }
  });
});
