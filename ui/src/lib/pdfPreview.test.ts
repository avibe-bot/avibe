import { describe, expect, it } from 'vitest';

import {
  PDF_MAX_CANVAS_PIXELS,
  PDF_MAX_ZOOM,
  PDF_MIN_ZOOM,
  PDF_ZOOM_STEPS,
  clampPdfZoom,
  pageIndexAt,
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
