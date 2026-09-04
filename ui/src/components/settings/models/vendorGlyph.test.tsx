// @vitest-environment jsdom
// A mark is only worth drawing if it tells one vendor from another, so the
// properties held here are drawn-at-all and drawn-once — asserted over whatever
// the shipped catalog contains rather than over a list restated in the test,
// which would keep passing for the twelve rows it knew about.
//
// The size properties are here for the same reason. Marks arrive filling
// anything from 70% to 100% of the box they were authored in, so what the CSS
// slot renders is one height only if every mark is re-boxed to the same ink
// fraction first — and that is checkable without a browser, because the box is
// in the DOM and the ink is recorded beside the path. What no jsdom test can
// see is a cap height a font actually produces; that is what the proof page in
// the PR is for.
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { API_KEY_VENDOR_PRESETS, CUSTOM_VENDOR } from './apiKeyVendors';
import { VendorGlyph } from './vendorGlyph';
import { VENDOR_MARKS } from './vendorMarks';

afterEach(cleanup);

/** Every choice the 服务商 field offers, including the one that is not a vendor. */
const CHOICES = [CUSTOM_VENDOR, ...API_KEY_VENDOR_PRESETS.map((preset) => preset.id)];

/** The rule `vendorMarks.ts` boxes by, restated so that changing it there has
 *  to be a decision here too: ink filling 19 of the 24 units of a 10:7 box. */
const INK_FRACTION = 19 / 24;
const BOX_ASPECT = 10 / 7;

const glyphOf = (vendor: string) => {
  const { container, unmount } = render(<VendorGlyph vendor={vendor} />);
  const svg = container.querySelector('svg');
  const [x, y, width, height] = (svg?.getAttribute('viewBox') ?? '').split(' ').map(Number);
  const drawn = {
    className: svg?.getAttribute('class') ?? '',
    hidden: svg?.getAttribute('aria-hidden'),
    art: svg?.innerHTML ?? '',
    markup: svg?.outerHTML ?? '',
    box: { x, y, width, height },
    letter: svg?.querySelector('text'),
  };
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

  it('gives every choice a box of one shape, so one CSS height is one ink height', () => {
    for (const vendor of CHOICES) {
      const { box, letter } = glyphOf(vendor);
      expect(box.width / box.height, `${vendor} is drawn in a box of its own shape`)
        .toBeCloseTo(BOX_ASPECT, 3);
      // A letter stands in for solid artwork, so it has to carry a comparable
      // weight: at the same height, a regular one still reads as the lighter
      // neighbour, which is half of what made the letters look wrong at all.
      if (letter) expect(letter.getAttribute('font-weight'), `${vendor} draws a light letter`).toBe('700');
    }
  });

  it('centres every mark inside that box at the one ink height, and none overflows it', () => {
    for (const [vendor, mark] of Object.entries(VENDOR_MARKS)) {
      const [x, y, width, height] = mark!.ink;
      const { box } = glyphOf(vendor);
      expect(height / box.height, `${vendor} puts a different amount of ink on screen`)
        .toBeCloseTo(INK_FRACTION, 3);
      expect(x + width / 2, `${vendor} sits off-centre horizontally`)
        .toBeCloseTo(box.x + box.width / 2, 2);
      expect(y + height / 2, `${vendor} sits off-centre vertically`)
        .toBeCloseTo(box.y + box.height / 2, 2);
      // Ink wider than its box is ink the slot clips. Nothing shipped is close,
      // but a wordmark could be: the box is 1.80 ink-widths per ink-height, and
      // artwork past that needs a wider slot, not a quiet crop.
      expect(width, `${vendor} is too wide for the shared slot`).toBeLessThanOrEqual(box.width);
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
