/* @vitest-environment jsdom */

import { afterEach, describe, expect, it } from 'vitest';

import { PDF_TEXT_LAYER_CLASS, trackPdfTextLayer } from './pdfTextSelection';

const stops: Array<() => void> = [];

const layerWith = (...words: string[]) => {
  const layer = document.createElement('div');
  layer.className = PDF_TEXT_LAYER_CLASS;
  for (const word of words) {
    const span = document.createElement('span');
    span.textContent = word;
    layer.append(span);
  }
  document.body.append(layer);
  stops.push(trackPdfTextLayer(layer));
  return layer;
};

const end = (layer: HTMLElement) => layer.querySelector<HTMLElement>('.vr-pdf-text-end');

afterEach(() => {
  stops.splice(0).forEach((stop) => stop());
  document.getSelection()?.removeAllRanges();
  document.body.replaceChildren();
});

describe('trackPdfTextLayer', () => {
  it('gives each layer an end-of-content block after its text', () => {
    const layer = layerWith('Page', 'one');
    expect(layer.lastElementChild).toBe(end(layer));
  });

  it('marks a layer as selecting from the press until the pointer is released', () => {
    const layer = layerWith('Page');
    layer.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    expect(layer.classList.contains('selecting')).toBe(true);
    document.dispatchEvent(new Event('pointerup'));
    expect(layer.classList.contains('selecting')).toBe(false);
  });

  it('marks only the layers the selection reaches', () => {
    const first = layerWith('Page', 'one');
    const second = layerWith('Page', 'two');
    const range = document.createRange();
    range.selectNodeContents(first.firstElementChild!);
    document.getSelection()!.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
    expect(first.classList.contains('selecting')).toBe(true);
    expect(second.classList.contains('selecting')).toBe(false);

    document.getSelection()!.removeAllRanges();
    document.dispatchEvent(new Event('selectionchange'));
    expect(first.classList.contains('selecting')).toBe(false);
    expect(first.lastElementChild).toBe(end(first));
  });

  it('leaves a released layer alone, and tracks layers again after every one was released', () => {
    const released = layerWith('Page', 'one');
    const kept = layerWith('Page', 'two');
    stops.shift()!();
    const selectFirstWord = (layer: HTMLElement) => {
      const range = document.createRange();
      range.selectNodeContents(layer.firstElementChild!);
      document.getSelection()!.removeAllRanges();
      document.getSelection()!.addRange(range);
      document.dispatchEvent(new Event('selectionchange'));
    };
    selectFirstWord(released);
    expect(released.classList.contains('selecting')).toBe(false);
    selectFirstWord(kept);
    expect(kept.classList.contains('selecting')).toBe(true);

    // Releasing the last layer stops the shared listeners; the next layer must start them again
    // (a reopened preview, or the first page drawn after scrolling back).
    stops.shift()!();
    const next = layerWith('Page', 'three');
    selectFirstWord(next);
    expect(next.classList.contains('selecting')).toBe(true);
  });
});
