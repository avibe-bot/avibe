import { describe, expect, it } from 'vitest';

import {
  PDF_MAX_CANVAS_PIXELS,
  PDF_MAX_ZOOM,
  PDF_MIN_ZOOM,
  PDF_ZOOM_STEPS,
  clampPdfZoom,
  pageIndexAt,
  pdfDestAxes,
  pdfDestPoint,
  pdfLinkAreas,
  pdfLinkTarget,
  pdfOutputScale,
  shouldRenderPdfPages,
  stepPdfZoom,
} from './pdfPreview';

describe('shouldRenderPdfPages', () => {
  const desktop = { pdfViewerEnabled: true, ios: false, coarsePointer: false };

  it('keeps the built-in viewer for a desktop browser that has one', () => {
    expect(shouldRenderPdfPages(desktop)).toBe(false);
    // Older engines that do not report the flag are assumed to frame PDFs as before.
    expect(shouldRenderPdfPages({ ...desktop, pdfViewerEnabled: undefined })).toBe(false);
  });

  it('draws the pages itself where the browser has no viewer (Android Chrome)', () => {
    expect(shouldRenderPdfPages({ ...desktop, pdfViewerEnabled: false })).toBe(true);
    expect(shouldRenderPdfPages({ pdfViewerEnabled: false, ios: false, coarsePointer: true })).toBe(true);
  });

  it('draws the pages itself on iOS and iPadOS, which report a viewer that shows only page one', () => {
    expect(shouldRenderPdfPages({ ...desktop, ios: true })).toBe(true);
    // iPadOS with a trackpad still reports a fine pointer.
    expect(shouldRenderPdfPages({ pdfViewerEnabled: true, ios: true, coarsePointer: false })).toBe(true);
  });

  it('draws the pages itself on other touch-first devices', () => {
    expect(shouldRenderPdfPages({ ...desktop, coarsePointer: true })).toBe(true);
  });
});

describe('PDF zoom', () => {
  it('clamps to the fit-width range', () => {
    expect(clampPdfZoom(0.5)).toBe(PDF_MIN_ZOOM);
    expect(clampPdfZoom(1.8)).toBe(1.8);
    expect(clampPdfZoom(9)).toBe(PDF_MAX_ZOOM);
    expect(clampPdfZoom(Number.NaN)).toBe(PDF_MIN_ZOOM);
  });

  it('steps along the ladder from any zoom, including one reached by pinching', () => {
    expect(stepPdfZoom(1, 1)).toBe(1.25);
    expect(stepPdfZoom(1.25, 1)).toBe(1.5);
    expect(stepPdfZoom(1.7, 1)).toBe(2);
    expect(stepPdfZoom(1.7, -1)).toBe(1.5);
    expect(stepPdfZoom(2, -1)).toBe(1.5);
    expect(stepPdfZoom(PDF_MAX_ZOOM, 1)).toBe(PDF_MAX_ZOOM);
    expect(stepPdfZoom(PDF_MIN_ZOOM, -1)).toBe(PDF_MIN_ZOOM);
  });

  it('walks every step up and back down', () => {
    const up: number[] = [];
    for (let z: number = PDF_MIN_ZOOM; z < PDF_MAX_ZOOM; z = stepPdfZoom(z, 1)) up.push(z);
    expect([...up, PDF_MAX_ZOOM]).toEqual([...PDF_ZOOM_STEPS]);
  });
});

describe('pdfOutputScale', () => {
  it('uses the display pixel ratio while the bitmap fits the budget', () => {
    expect(pdfOutputScale(390, 505, 3)).toBe(3);
    expect(pdfOutputScale(390, 505, 1)).toBe(1);
  });

  it('caps the bitmap area for zoomed-in pages', () => {
    const scale = pdfOutputScale(1560, 2020, 3);
    expect(scale).toBeLessThan(3);
    expect(1560 * scale * 2020 * scale).toBeLessThanOrEqual(PDF_MAX_CANVAS_PIXELS * 1.0001);
  });

  it('treats a missing or invalid pixel ratio as 1', () => {
    expect(pdfOutputScale(390, 505, 0)).toBe(1);
    expect(pdfOutputScale(390, 505, Number.NaN)).toBe(1);
  });
});

describe('pageIndexAt', () => {
  const tops = [12, 520, 1028, 1536];

  it('finds the page whose top is the last one at or above the point', () => {
    expect(pageIndexAt(tops, 0)).toBe(0);
    expect(pageIndexAt(tops, 12)).toBe(0);
    expect(pageIndexAt(tops, 519)).toBe(0);
    expect(pageIndexAt(tops, 520)).toBe(1);
    expect(pageIndexAt(tops, 1200)).toBe(2);
    expect(pageIndexAt(tops, 99999)).toBe(3);
  });

  it('is 0 for an empty document', () => {
    expect(pageIndexAt([], 100)).toBe(0);
  });
});

describe('pdfLinkTarget', () => {
  const base = 'https://avibe.example/chat/s1';
  const link = (over: Record<string, unknown>) => ({ subtype: 'Link', rect: [0, 0, 10, 10], ...over });

  it('opens web and mail links', () => {
    expect(pdfLinkTarget(link({ url: 'https://example.com/a' }), base)).toEqual({ kind: 'url', href: 'https://example.com/a' });
    expect(pdfLinkTarget(link({ url: 'mailto:team@example.com' }), base)).toEqual({ kind: 'url', href: 'mailto:team@example.com' });
  });

  it('offers no link for other schemes pdf.js still accepts', () => {
    expect(pdfLinkTarget(link({ url: 'tel:+15550100' }), base)).toBeNull();
    expect(pdfLinkTarget(link({ url: 'ftp://files.example.com/x' }), base)).toBeNull();
    // pdf.js puts a rejected URI in ``unsafeUrl`` and leaves ``url`` unset.
    expect(pdfLinkTarget(link({ unsafeUrl: 'javascript:alert(1)' }), base)).toBeNull();
  });

  it('follows named and explicit destinations inside the document', () => {
    expect(pdfLinkTarget(link({ dest: 'chapter-2' }), base)).toEqual({ kind: 'dest', dest: 'chapter-2' });
    const explicit = [{ num: 9, gen: 0 }, { name: 'XYZ' }, 0, 400, null];
    expect(pdfLinkTarget(link({ dest: explicit }), base)).toEqual({ kind: 'dest', dest: explicit });
  });

  it('ignores hidden links, other annotation types, and links without a target', () => {
    expect(pdfLinkTarget(link({ url: 'https://example.com', annotationFlags: 0x2 }), base)).toBeNull();
    expect(pdfLinkTarget({ subtype: 'Text', url: 'https://example.com' }, base)).toBeNull();
    expect(pdfLinkTarget(link({ dest: [] }), base)).toBeNull();
    expect(pdfLinkTarget(link({}), base)).toBeNull();
  });
});

describe('pdfLinkAreas', () => {
  it('uses the rectangle, normalized, when there are no quadrilaterals', () => {
    expect(pdfLinkAreas({ rect: [212, 204, 70, 172] })).toEqual([[70, 172, 212, 204]]);
  });

  it('gives a link that wraps onto a second line one area per line', () => {
    const quadPoints = new Float32Array([400, 220, 560, 220, 400, 200, 560, 200, 72, 190, 180, 190, 72, 170, 180, 170]);
    expect(pdfLinkAreas({ rect: [72, 170, 560, 220], quadPoints })).toEqual([
      [400, 200, 560, 220],
      [72, 170, 180, 190],
    ]);
  });

  it('has no area without geometry', () => {
    expect(pdfLinkAreas({ rect: null })).toEqual([]);
  });
});

describe('pdfDestPoint', () => {
  const page = { num: 3, gen: 0 };
  it.each([
    [[page, { name: 'XYZ' }, 36, 400, null], { x: 36, y: 400 }],
    [[page, { name: 'XYZ' }, null, 400, 0], { x: null, y: 400 }],
    [[page, { name: 'FitH' }, 500], { x: null, y: 500 }],
    [[page, { name: 'FitBH' }, 500], { x: null, y: 500 }],
    [[page, { name: 'FitV' }, 20], { x: 20, y: null }],
    [[page, { name: 'FitR' }, 10, 20, 300, 400], { x: 10, y: 400 }],
    [[page, { name: 'Fit' }], { x: null, y: null }],
    [[page], { x: null, y: null }],
  ])('reads %j', (dest, point) => {
    expect(pdfDestPoint(dest)).toEqual(point);
  });
});

describe('pdfDestAxes', () => {
  it('fixes the axes the destination names on an upright or upside-down page', () => {
    expect(pdfDestAxes({ x: null, y: 400 }, 0)).toEqual({ left: false, top: true });
    expect(pdfDestAxes({ x: 36, y: null }, 180)).toEqual({ left: true, top: false });
  });

  it('swaps them on a page turned a quarter either way', () => {
    // FitH names only y, which a 90° page shows across the screen.
    expect(pdfDestAxes({ x: null, y: 400 }, 90)).toEqual({ left: true, top: false });
    expect(pdfDestAxes({ x: 36, y: null }, 270)).toEqual({ left: false, top: true });
    expect(pdfDestAxes({ x: 36, y: 400 }, -90)).toEqual({ left: true, top: true });
  });
});
