/* @vitest-environment jsdom */

import { afterEach, describe, expect, it } from 'vitest';

import { PDF_TEXT_LAYER_CLASS, trackPdfTextLayer, trackedPdfTextLayerCount } from './pdfTextSelection';

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

  it('stops tracking a layer and drops the document listeners with the last one', () => {
    const layer = layerWith('Page');
    layerWith('Other');
    expect(trackedPdfTextLayerCount()).toBe(2);
    stops.splice(0).forEach((stop) => stop());
    expect(trackedPdfTextLayerCount()).toBe(0);
    // Listeners are gone: a press no longer reaches the released layer.
    layer.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    expect(layer.classList.contains('selecting')).toBe(false);
  });
});
