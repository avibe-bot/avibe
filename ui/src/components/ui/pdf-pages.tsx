import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Maximize2, ZoomIn, ZoomOut } from 'lucide-react';
import clsx from 'clsx';
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFWorker, RenderTask, TextLayer } from 'pdfjs-dist/legacy/build/pdf.mjs';
import PdfjsWorker from 'pdfjs-dist/legacy/build/pdf.worker.mjs?worker';

import { Button } from '@/components/ui/button';
import {
  PDF_MAX_ZOOM,
  PDF_MIN_ZOOM,
  type PdfLinkAnnotation,
  type PdfLinkTarget,
  clampPdfZoom,
  pageIndexAt,
  pdfDestAxes,
  pdfDestPoint,
  pdfLinkAreas,
  pdfLinkTarget,
  pdfOutputScale,
  stepPdfZoom,
} from '@/lib/pdfPreview';
import { unifyCjkRadicals } from '@/lib/cjkRadicals';
import { PDF_TEXT_LAYER_CLASS, trackPdfTextLayer } from '@/lib/pdfTextSelection';

// ── PDF pages ────────────────────────────────────────────────────────────────
// Every page of a PDF drawn to its own canvas by pdf.js, stacked in one scroller: the reader used
// where framing the browser's viewer does not work (see ``shouldRenderPdfPages``). Default export so
// the file-preview kernel can ``React.lazy`` it; pdf.js, its worker and its data files load only when
// a PDF is opened here.
//
// - Pages are fit to the viewer's width; zoom (buttons, pinch, ctrl-wheel) scales that width and
//   keeps the point under the fingers / centre in place.
// - Only pages within a screen and a half of the viewport hold a canvas. Others are released, which
//   is what keeps a long document inside iOS's canvas memory budget.
// - Each drawn page carries pdf.js's text layer: transparent text laid exactly over the drawing, so
//   the text can be long-pressed or dragged to select and copied (styles: ``.vr-pdf-text`` in
//   index.css; selection behaviour: lib/pdfTextSelection.ts).
// - Link annotations become anchors over their areas: web and mail links open in a new tab, links
//   to a place in the document scroll the reader there. Other actions are not offered.
// - The legacy build is used on purpose: the modern one relies on JavaScript features newer than the
//   mobile Safari versions people still run.

// pdf.js asks for its CMaps (needed by many CJK PDFs), standard fonts and image-decoder wasm by file
// name. They are bundled as hashed assets and served from this table, so no unhashed directory has
// to exist beside the build and a stale tab never mixes files from two pdf.js versions.
const byFileName = (modules: Record<string, string>): Record<string, string> =>
  Object.fromEntries(Object.entries(modules).map(([path, url]) => [path.slice(path.lastIndexOf('/') + 1), url]));

const BUNDLED_DATA: Record<string, Record<string, string>> = {
  cMapUrl: byFileName(import.meta.glob<string>('/node_modules/pdfjs-dist/cmaps/*.bcmap', { query: '?url', import: 'default', eager: true })),
  standardFontDataUrl: byFileName(
    import.meta.glob<string>(['/node_modules/pdfjs-dist/standard_fonts/*.pfb', '/node_modules/pdfjs-dist/standard_fonts/*.ttf'], { query: '?url', import: 'default', eager: true }),
  ),
  wasmUrl: byFileName(import.meta.glob<string>(['/node_modules/pdfjs-dist/wasm/*.wasm', '!**/quickjs-*'], { query: '?url', import: 'default', eager: true })),
};

// pdf.js's ``BinaryDataFactory`` contract: constructed with its base URLs (unused here) and asked to
// ``fetch({ kind, filename })`` the bytes of one data file.
class BundledDataFactory {
  async fetch({ kind, filename }: { kind: string; filename: string }): Promise<Uint8Array> {
    const url = BUNDLED_DATA[kind]?.[filename];
    if (!url) throw new Error(`pdf.js data file is not bundled: ${filename}`);
    const res = await fetch(url);
    if (!res.ok) throw new Error(`pdf.js data file ${filename}: HTTP ${res.status}`);
    return new Uint8Array(await res.arrayBuffer());
  }
}

// Pages this far (as a fraction of the viewport) above or below the visible area keep a canvas.
const NEAR_MARGIN = '150% 0px';
// Horizontal padding around the page column (px-3 on each side), taken out of the fit width.
const PAGE_GUTTER = 24;
const WHEEL_ZOOM_SENSITIVITY = 0.01;
// Overlay buttons on the dark mat; matches the image preview's control cluster in file-preview.tsx.
const ZOOM_BTN = 'h-8 w-8 rounded-full text-white hover:bg-white/15 hover:text-white';

// One IntersectionObserver for every page, rooted at the scroller. Pages register before the
// scroller's effect attaches it (child effects run first), so ``attach`` observes what is waiting.
class PageWatch {
  private io: IntersectionObserver | null = null;
  private readonly callbacks = new Map<Element, (near: boolean) => void>();

  attach(root: HTMLElement): () => void {
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) this.callbacks.get(entry.target)?.(entry.isIntersecting);
      },
      { root, rootMargin: NEAR_MARGIN },
    );
    this.io = io;
    for (const el of this.callbacks.keys()) io.observe(el);
    return () => {
      io.disconnect();
      if (this.io === io) this.io = null;
    };
  }

  observe(el: Element, onChange: (near: boolean) => void): () => void {
    this.callbacks.set(el, onChange);
    this.io?.observe(el);
    return () => {
      this.callbacks.delete(el);
      this.io?.unobserve(el);
    };
  }
}

type TextLayerClass = typeof TextLayer;

type DocState = { status: 'loading' } | { status: 'error' } | { status: 'ready'; doc: PDFDocumentProxy; ratio: number; TextLayer: TextLayerClass };

type Anchor = { page: number; fx: number; fy: number; ax: number; ay: number };

const pageElements = (content: HTMLElement | null): HTMLElement[] =>
  content ? Array.from(content.querySelectorAll<HTMLElement>('[data-pdf-page]')) : [];

export default function PdfPages({ url, className }: { url: string; className?: string }) {
  const { t } = useTranslation();
  const [state, setState] = React.useState<DocState>({ status: 'loading' });

  // Callers key this component by URL, so a new file always starts from 'loading'.
  React.useEffect(() => {
    let alive = true;
    let port: Worker | null = null;
    let worker: PDFWorker | null = null;
    let task: PDFDocumentLoadingTask | null = null;
    let textLayers: TextLayerClass | null = null;
    (async () => {
      try {
        const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
        textLayers = pdfjs.TextLayer;
        if (!alive) return;
        // A worker per document, terminated with it, so closing the preview gives its memory back.
        // (pdf.js's generated typings declare ``port`` as null-only; it takes a Worker at runtime.)
        port = new PdfjsWorker();
        worker = new pdfjs.PDFWorker({ port } as unknown as ConstructorParameters<typeof pdfjs.PDFWorker>[0]);
        task = pdfjs.getDocument({
          // Same-origin content URL: pdf.js fetches it with the session cookie, by ranges when the
          // server allows, so the first page can draw before a large file has fully arrived.
          url: new URL(url, window.location.href).href,
          worker,
          useWorkerFetch: false,
          BinaryDataFactory: BundledDataFactory,
        });
        const doc = await task.promise;
        const first = await doc.getPage(1);
        const viewport = first.getViewport({ scale: 1 });
        if (alive) setState({ status: 'ready', doc, ratio: viewport.height / viewport.width, TextLayer: pdfjs.TextLayer });
      } catch {
        if (alive) setState({ status: 'error' });
      }
    })();
    return () => {
      alive = false;
      const pending = task ? task.destroy() : Promise.resolve();
      void pending
        .catch(() => undefined)
        .finally(() => {
          worker?.destroy();
          port?.terminate();
          // Drops the hidden measuring canvas pdf.js keeps in <body> once no text layer is pending.
          textLayers?.cleanup();
        });
    };
  }, [url]);

  if (state.status === 'error') {
    return <div className={clsx('grid h-full min-h-0 place-items-center bg-surface p-6 text-center text-[12.5px] text-muted', className)}>{t('preview.failed')}</div>;
  }
  if (state.status === 'loading') {
    return (
      <div className={clsx('grid h-full min-h-0 place-items-center bg-[#0c0c0f] text-muted', className)} data-pdf-loading>
        <Loader2 className="size-5 animate-spin" />
      </div>
    );
  }
  return <PdfReader doc={state.doc} ratio={state.ratio} TextLayer={state.TextLayer} className={className} />;
}

function PdfReader({ doc, ratio, TextLayer, className }: { doc: PDFDocumentProxy; ratio: number; TextLayer: TextLayerClass; className?: string }) {
  const { t } = useTranslation();
  const [scroller, setScroller] = React.useState<HTMLDivElement | null>(null);
  const contentRef = React.useRef<HTMLDivElement>(null);
  const [watch] = React.useState(() => new PageWatch());
  const [fitWidth, setFitWidth] = React.useState(0);
  const [zoom, setZoom] = React.useState(1);
  const zoomRef = React.useRef(1);
  const anchorRef = React.useRef<Anchor | null>(null);
  const [current, setCurrent] = React.useState(1);
  const total = doc.numPages;
  const pageWidth = Math.max(0, Math.round(fitWidth * zoom));

  React.useEffect(() => {
    if (!scroller) return;
    return watch.attach(scroller);
  }, [scroller, watch]);

  // Fit width follows the scroller (sheet resize, rotation). The observer reports once on observe.
  React.useEffect(() => {
    if (!scroller || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setFitWidth(Math.max(0, Math.floor(scroller.clientWidth - PAGE_GUTTER))));
    ro.observe(scroller);
    return () => ro.disconnect();
  }, [scroller]);

  // The page indicator: the page crossing 30% of the viewport's height.
  React.useEffect(() => {
    if (!scroller) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      const pages = pageElements(contentRef.current);
      if (!pages.length) return;
      const tops = pages.map((p) => p.offsetTop);
      setCurrent(pageIndexAt(tops, scroller.scrollTop + scroller.clientHeight * 0.3) + 1);
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    scroller.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      scroller.removeEventListener('scroll', onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [scroller]);

  // Zoom about a point of the scroller's viewport (default: its centre). The point is remembered as
  // a position within the page under it, then restored after layout, so it stays put on screen
  // regardless of the fixed gaps between pages.
  const applyZoom = React.useCallback(
    (next: number, ax?: number, ay?: number) => {
      const target = clampPdfZoom(next);
      if (!scroller || Math.abs(target - zoomRef.current) < 0.001) return;
      const x = ax ?? scroller.clientWidth / 2;
      const y = ay ?? scroller.clientHeight / 2;
      const pages = pageElements(contentRef.current);
      if (pages.length) {
        const cx = scroller.scrollLeft + x;
        const cy = scroller.scrollTop + y;
        const index = pageIndexAt(pages.map((p) => p.offsetTop), cy);
        const page = pages[index];
        anchorRef.current = {
          page: index,
          fx: (cx - page.offsetLeft) / Math.max(1, page.offsetWidth),
          fy: (cy - page.offsetTop) / Math.max(1, page.offsetHeight),
          ax: x,
          ay: y,
        };
      }
      zoomRef.current = target;
      setZoom(target);
    },
    [scroller],
  );

  React.useLayoutEffect(() => {
    const anchor = anchorRef.current;
    if (!anchor || !scroller) return;
    anchorRef.current = null;
    const page = pageElements(contentRef.current)[anchor.page];
    if (!page) return;
    scroller.scrollTo({
      left: page.offsetLeft + anchor.fx * page.offsetWidth - anchor.ax,
      top: page.offsetTop + anchor.fy * page.offsetHeight - anchor.ay,
    });
  }, [zoom, scroller]);

  // Pinch zoom on touch screens and ctrl-wheel (trackpad pinch) elsewhere. While the fingers move the
  // column is only scaled with a transform; the zoom, and the re-render it costs, lands on release.
  // Single-finger scrolling stays native. The app's viewport disables page zoom, so without this a
  // phone could not zoom the document at all.
  React.useEffect(() => {
    const el = scroller;
    const content = contentRef.current;
    if (!el || !content) return;
    let pinch: { dist: number; ax: number; ay: number } | null = null;
    let ratio = 1;
    const spread = (touches: TouchList) => Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
    const reset = () => {
      content.style.transform = '';
      content.style.transformOrigin = '';
    };
    const onStart = (e: TouchEvent) => {
      if (e.touches.length !== 2) return;
      const rect = el.getBoundingClientRect();
      const ax = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
      const ay = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
      pinch = { dist: Math.max(1, spread(e.touches)), ax, ay };
      ratio = 1;
      content.style.transformOrigin = `${el.scrollLeft + ax}px ${el.scrollTop + ay}px`;
    };
    const onMove = (e: TouchEvent) => {
      if (!pinch || e.touches.length !== 2) return;
      e.preventDefault();
      const z = zoomRef.current;
      ratio = clampPdfZoom((z * spread(e.touches)) / pinch.dist) / z;
      content.style.transform = `scale(${ratio})`;
    };
    const onEnd = (e: TouchEvent) => {
      if (!pinch || e.touches.length >= 2) return;
      const { ax, ay } = pinch;
      pinch = null;
      reset();
      if (Math.abs(ratio - 1) > 0.01) applyZoom(zoomRef.current * ratio, ax, ay);
      ratio = 1;
    };
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      applyZoom(zoomRef.current * Math.exp(-e.deltaY * WHEEL_ZOOM_SENSITIVITY), e.clientX - rect.left, e.clientY - rect.top);
    };
    // iOS Safari starts its own page zoom from these unless told not to.
    const onGesture = (e: Event) => e.preventDefault();
    el.addEventListener('touchstart', onStart, { passive: true });
    el.addEventListener('touchmove', onMove, { passive: false });
    el.addEventListener('touchend', onEnd);
    el.addEventListener('touchcancel', onEnd);
    el.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('gesturestart', onGesture);
    return () => {
      el.removeEventListener('touchstart', onStart);
      el.removeEventListener('touchmove', onMove);
      el.removeEventListener('touchend', onEnd);
      el.removeEventListener('touchcancel', onEnd);
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('gesturestart', onGesture);
      reset();
    };
  }, [scroller, applyZoom]);

  // Follow a link to a place in the document: scroll its page in, at the point the destination
  // names when it names one, a little below the top edge so that line is not flush with it.
  const goTo = React.useCallback(
    async (dest: string | readonly unknown[]) => {
      try {
        const explicit = typeof dest === 'string' ? await doc.getDestination(dest) : dest;
        if (!scroller || !Array.isArray(explicit) || explicit.length === 0) return;
        const ref: unknown = explicit[0];
        const index =
          typeof ref === 'number' ? ref : ref && typeof ref === 'object' ? await doc.getPageIndex(ref as Parameters<PDFDocumentProxy['getPageIndex']>[0]) : -1;
        const pageEl = pageElements(contentRef.current)[index];
        if (!pageEl) return;
        const page = await doc.getPage(index + 1);
        const viewport = page.getViewport({ scale: pageEl.offsetWidth / page.getViewport({ scale: 1 }).width });
        const point = pdfDestPoint(explicit);
        // On a page with /Rotate 90 or 270 the destination's x and y land on the other screen axes.
        const axes = pdfDestAxes(point, viewport.rotation);
        const [vx, vy] = viewport.convertToViewportPoint(point.x ?? 0, point.y ?? 0) as [number, number];
        scroller.scrollTo({
          top: pageEl.offsetTop + (axes.top ? Math.max(0, vy - 8) : 0),
          left: axes.left ? Math.max(0, pageEl.offsetLeft + vx - 8) : scroller.scrollLeft,
        });
      } catch {
        // A destination that does not resolve leaves the reader where it is.
      }
    },
    [doc, scroller],
  );

  const pages = React.useMemo(() => Array.from({ length: total }, (_, i) => i + 1), [total]);
  const atFit = zoom <= PDF_MIN_ZOOM + 0.001;
  const atMax = zoom >= PDF_MAX_ZOOM - 0.001;

  return (
    <div className={clsx('relative h-full min-h-0 overflow-hidden bg-[#0c0c0f]', className)}>
      <div ref={setScroller} className="h-full overflow-auto overscroll-contain" style={{ touchAction: 'pan-x pan-y' }} data-pdf-scroller>
        {/* ``relative``: the pages' offsets are measured against this column, the scroller's content. */}
        <div ref={contentRef} className="relative flex w-max min-w-full flex-col items-center gap-3 px-3 pb-16 pt-3">
          {pageWidth > 0 &&
            pages.map((n) => (
              <PdfPage
                key={n}
                doc={doc}
                TextLayer={TextLayer}
                pageNumber={n}
                width={pageWidth}
                fallbackRatio={ratio}
                watch={watch}
                onLink={goTo}
                label={t('preview.pdfPage', { page: n })}
                linkLabel={t('preview.pdfInternalLink')}
              />
            ))}
        </div>
      </div>
      {/* Control cluster, as in the image preview; the bottom padding of the column keeps the last
          page scrollable clear of it, and the safe-area inset keeps it off the home indicator. */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-center p-3" style={{ paddingBottom: 'max(0.75rem, env(safe-area-inset-bottom))' }}>
        <div className="pointer-events-auto flex items-center gap-0.5 rounded-full bg-black/55 px-1 py-1 text-white shadow-lg backdrop-blur-sm">
          <Button variant="ghost" size="icon" className={ZOOM_BTN} onClick={() => applyZoom(stepPdfZoom(zoomRef.current, -1))} disabled={atFit} aria-label={t('preview.zoomOut')}>
            <ZoomOut className="size-4" />
          </Button>
          <span className="min-w-[3.25rem] text-center text-[11px] font-medium tabular-nums" aria-live="polite">
            {t('preview.zoomPercent', { percent: Math.round(zoom * 100) })}
          </span>
          <Button variant="ghost" size="icon" className={ZOOM_BTN} onClick={() => applyZoom(stepPdfZoom(zoomRef.current, 1))} disabled={atMax} aria-label={t('preview.zoomIn')}>
            <ZoomIn className="size-4" />
          </Button>
          <span className="mx-0.5 h-4 w-px bg-white/20" aria-hidden />
          <Button variant="ghost" size="icon" className={ZOOM_BTN} onClick={() => applyZoom(PDF_MIN_ZOOM)} disabled={atFit} aria-label={t('preview.resetZoom')}>
            <Maximize2 className="size-4" />
          </Button>
          <span className="mx-0.5 h-4 w-px bg-white/20" aria-hidden />
          <span className="px-2 text-[11px] font-medium tabular-nums" role="status" aria-label={t('preview.pdfPosition', { page: current, total })} data-pdf-position>
            {t('preview.pdfCounter', { page: current, total })}
          </span>
        </div>
      </div>
    </div>
  );
}

// What a page box shows: its canvas, text layer and link layer, swapped in together. The previous
// text layer stops being tracked for selection when it leaves the document.
const trackedLayers = new WeakMap<HTMLElement, () => void>();

function showPage(box: HTMLElement, canvas: HTMLCanvasElement | null, layer: HTMLElement | null, links: HTMLElement | null = null) {
  trackedLayers.get(box)?.();
  trackedLayers.delete(box);
  box.replaceChildren(...[canvas, layer, links].filter((node): node is HTMLCanvasElement | HTMLElement => node !== null));
  if (layer) trackedLayers.set(box, trackPdfTextLayer(layer));
}

// One anchor per link area, positioned in percent of the page so it stays on its words while the
// box is resized ahead of the next render.
function linkLayer(
  annotations: readonly PdfLinkAnnotation[],
  viewport: { width: number; height: number; convertToViewportPoint: (x: number, y: number) => number[] },
  onLink: (dest: string | readonly unknown[]) => void,
  base: string,
  internalLabel: string,
): HTMLElement | null {
  const anchors: HTMLAnchorElement[] = [];
  for (const annotation of annotations) {
    const target: PdfLinkTarget | null = pdfLinkTarget(annotation, base);
    if (!target) continue;
    for (const area of pdfLinkAreas(annotation)) {
      const [x1, y1] = viewport.convertToViewportPoint(area[0], area[1]);
      const [x2, y2] = viewport.convertToViewportPoint(area[2], area[3]);
      const a = document.createElement('a');
      a.style.left = `${(100 * Math.min(x1, x2)) / viewport.width}%`;
      a.style.top = `${(100 * Math.min(y1, y2)) / viewport.height}%`;
      a.style.width = `${(100 * Math.abs(x2 - x1)) / viewport.width}%`;
      a.style.height = `${(100 * Math.abs(y2 - y1)) / viewport.height}%`;
      if (target.kind === 'url') {
        a.href = target.href;
        a.target = '_blank';
        a.rel = 'noopener noreferrer nofollow';
        a.title = target.href;
      } else {
        const { dest } = target;
        a.href = '#';
        a.dataset.pdfDest = 'true';
        a.setAttribute('aria-label', internalLabel);
        a.addEventListener('click', (event) => {
          event.preventDefault();
          onLink(dest);
        });
      }
      anchors.push(a);
    }
  }
  if (anchors.length === 0) return null;
  const layer = document.createElement('div');
  layer.className = 'vr-pdf-links';
  layer.append(...anchors);
  return layer;
}

const PdfPage = React.memo(function PdfPage({
  doc,
  TextLayer,
  pageNumber,
  width,
  fallbackRatio,
  watch,
  onLink,
  label,
  linkLabel,
}: {
  doc: PDFDocumentProxy;
  TextLayer: TextLayerClass;
  pageNumber: number;
  width: number;
  fallbackRatio: number;
  watch: PageWatch;
  onLink: (dest: string | readonly unknown[]) => void;
  label: string;
  linkLabel: string;
}) {
  const boxRef = React.useRef<HTMLDivElement>(null);
  const [near, setNear] = React.useState(false);
  // The page's own proportions once loaded; until then the first page's stand in for its placeholder.
  const [ratio, setRatio] = React.useState<number | null>(null);

  React.useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const stop = watch.observe(box, setNear);
    return () => {
      stop();
      showPage(box, null, null);
    };
  }, [watch]);

  // The canvas and text layer live outside React's tree (the box has no React children), so a
  // re-render at a new width keeps showing the previous ones, stretched, until the new pair replaces
  // them together.
  React.useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    if (!near) {
      showPage(box, null, null);
      delete box.dataset.pdfRendered;
      delete box.dataset.pdfText;
      return;
    }
    let cancelled = false;
    let task: RenderTask | null = null;
    let text: TextLayer | null = null;
    (async () => {
      const page = await doc.getPage(pageNumber);
      if (cancelled) return;
      const base = page.getViewport({ scale: 1 });
      const pageRatio = base.height / base.width;
      setRatio(pageRatio);
      const cssScale = width / base.width;
      // The text layer sizes itself and its text from this variable. Set before drawing, so a layer
      // still showing from the previous width already follows the resized box.
      box.style.setProperty('--total-scale-factor', String(cssScale));
      const outputScale = pdfOutputScale(width, width * pageRatio, window.devicePixelRatio);
      const viewport = page.getViewport({ scale: cssScale * outputScale });
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.floor(viewport.width));
      canvas.height = Math.max(1, Math.floor(viewport.height));
      canvas.className = 'absolute inset-0 h-full w-full';
      task = page.render({ canvas, viewport });
      // Text is laid out in CSS pixels, in parallel with the drawing. A page whose text cannot be
      // extracted is still shown, just without selectable text. Radical look-alikes are unified
      // first, so what is selected copies as the characters on screen.
      const layer = document.createElement('div');
      layer.className = PDF_TEXT_LAYER_CLASS;
      const textReady = page
        .getTextContent()
        .then((content) => {
          if (cancelled) return false;
          for (const item of content.items) if ('str' in item) item.str = unifyCjkRadicals(item.str);
          text = new TextLayer({ textContentSource: content, container: layer, viewport: page.getViewport({ scale: cssScale }) });
          return text.render().then(() => true);
        })
        .catch(() => false);
      const links = page.getAnnotations({ intent: 'display' }).then(
        (annotations: PdfLinkAnnotation[]) => linkLayer(annotations, page.getViewport({ scale: cssScale }), onLink, window.location.href, linkLabel),
        () => null,
      );
      await task.promise;
      const hasText = await textReady;
      const linkLayerEl = await links;
      if (cancelled) return;
      showPage(box, canvas, hasText ? layer : null, linkLayerEl);
      box.dataset.pdfRendered = 'true';
      if (hasText) box.dataset.pdfText = 'true';
      else delete box.dataset.pdfText;
    })().catch(() => undefined); // cancelled renders reject; a page that fails to draw stays blank
    return () => {
      cancelled = true;
      task?.cancel();
      text?.cancel();
    };
  }, [doc, TextLayer, pageNumber, width, near, onLink, linkLabel]);

  // A group, not an image: the page's text is readable by assistive technology through its layer.
  return (
    <div
      ref={boxRef}
      role="group"
      aria-label={label}
      data-pdf-page={pageNumber}
      className="relative shrink-0 bg-white shadow-[0_1px_4px_rgba(0,0,0,0.5)]"
      style={{ width, height: Math.round(width * (ratio ?? fallbackRatio)), ['--scale-round-x' as string]: '1px', ['--scale-round-y' as string]: '1px' }}
    />
  );
});
