// Text selection over the PDF page reader's text layers (components/ui/pdf-pages.tsx).
//
// pdf.js places one transparent, absolutely positioned span per run of text over each page canvas,
// so the page's text can be long-pressed (touch) or dragged (mouse) to select and copy it. Between
// those spans there is only empty layer. WebKit — every iPhone and iPad — resolves a selection end
// dragged into that empty space to the start of the layer, so the selection collapses to nothing as
// soon as a finger or handle leaves the text. pdf.js's own viewer prevents that with an "end of
// content" block that it moves next to the selection's moving end while a selection is in progress;
// this is that technique, ported from its ``TextLayerBuilder`` (Apache-2.0) without the rest of the
// viewer.
//
// Current Chromium keeps the end on the nearest text by itself, and measured worse with the block in
// place, so there (and in Firefox, which pdf.js also exempts) the layers are left to the browser.

export const PDF_TEXT_LAYER_CLASS = 'vr-pdf-text';
const END_CLASS = 'vr-pdf-text-end';
const SELECTING_CLASS = 'selecting';

// Text layer → its end-of-content block, for every layer currently in the document.
const layers = new Map<HTMLElement, HTMLElement>();
let listeners: AbortController | null = null;

const reset = (end: HTMLElement, layer: HTMLElement) => {
  layer.append(end);
  end.style.width = '';
  end.style.height = '';
  end.style.userSelect = '';
  layer.classList.remove(SELECTING_CLASS);
};

// Firefox, and Chromium from 148 (the version pdf.js's viewer also keys on).
function handlesGapsNatively(): boolean {
  if (typeof CSS !== 'undefined' && typeof CSS.supports === 'function' && CSS.supports('-moz-user-select', 'none')) return true;
  const brands = (navigator as Navigator & { userAgentData?: { brands: { brand: string; version: string }[] } }).userAgentData?.brands;
  const chromium = brands ? brands.find(({ brand }) => brand === 'Chromium')?.version : /\bChrome\/(\d+)\b/.exec(navigator.userAgent)?.[1];
  return !!chromium && Number.parseInt(chromium, 10) >= 148;
}

function listen() {
  if (listeners) return;
  const controller = new AbortController();
  listeners = controller;
  const { signal } = controller;
  let pointerDown = false;
  let previous: Range | null = null;
  const resetAll = () => layers.forEach(reset);

  document.addEventListener('pointerdown', () => (pointerDown = true), { signal });
  document.addEventListener(
    'pointerup',
    () => {
      pointerDown = false;
      resetAll();
    },
    { signal },
  );
  window.addEventListener(
    'blur',
    () => {
      pointerDown = false;
      resetAll();
    },
    { signal },
  );
  document.addEventListener('keyup', () => !pointerDown && resetAll(), { signal });
  document.addEventListener(
    'selectionchange',
    () => {
      const selection = document.getSelection();
      if (!selection || selection.rangeCount === 0) {
        resetAll();
        return;
      }
      const active = new Set<HTMLElement>();
      for (let i = 0; i < selection.rangeCount; i += 1) {
        const range = selection.getRangeAt(i);
        for (const layer of layers.keys()) if (range.intersectsNode(layer)) active.add(layer);
      }
      for (const [layer, end] of layers) {
        if (active.has(layer)) layer.classList.add(SELECTING_CLASS);
        else reset(end, layer);
      }
      if (active.size === 0) return;

      // Move the end block right after the selection's moving end (or before it, when the start is
      // the end being dragged), so empty space past that point resolves to the block, not the layer.
      const range = selection.getRangeAt(0);
      const movingStart =
        !!previous &&
        (range.compareBoundaryPoints(Range.END_TO_END, previous) === 0 || range.compareBoundaryPoints(Range.START_TO_END, previous) === 0);
      let anchor: Node | null = movingStart ? range.startContainer : range.endContainer;
      if (anchor?.nodeType === Node.TEXT_NODE) anchor = anchor.parentNode;
      if (!movingStart && range.endOffset === 0 && anchor) {
        // The end sits before the anchor's first character: it belongs to the previous text.
        do {
          while (anchor && !anchor.previousSibling) anchor = anchor.parentNode;
          anchor = anchor?.previousSibling ?? null;
        } while (anchor && !anchor.childNodes.length);
      }
      const parent = anchor?.parentElement;
      const layer = parent?.closest<HTMLElement>(`.${PDF_TEXT_LAYER_CLASS}`);
      const end = layer ? layers.get(layer) : undefined;
      if (anchor && parent && layer && end) {
        end.style.width = layer.style.width;
        end.style.height = layer.style.height;
        end.style.userSelect = 'text';
        parent.insertBefore(end, movingStart ? anchor : anchor.nextSibling);
      }
      previous = range.cloneRange();
    },
    { signal },
  );
}

/**
 * Track a rendered text layer: appends its end-of-content block and starts the shared document
 * listeners on the first layer. Returns the function that stops tracking it, which also removes the
 * listeners once no layer is left. A no-op where the browser keeps selections on the text itself.
 */
export function trackPdfTextLayer(layer: HTMLElement): () => void {
  if (handlesGapsNatively()) return () => undefined;
  const end = document.createElement('div');
  end.className = END_CLASS;
  layer.append(end);
  const onMouseDown = () => layer.classList.add(SELECTING_CLASS);
  layer.addEventListener('mousedown', onMouseDown);
  layers.set(layer, end);
  listen();
  return () => {
    layer.removeEventListener('mousedown', onMouseDown);
    if (layers.get(layer) === end) layers.delete(layer);
    if (layers.size === 0) {
      listeners?.abort();
      listeners = null;
    }
  };
}

/** Layers currently tracked; for tests. */
export function trackedPdfTextLayerCount(): number {
  return layers.size;
}
