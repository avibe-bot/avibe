import { isIosDevice } from '@/lib/platform';

// Pure decisions behind the PDF preview (components/ui/pdf-pages.tsx), kept free of pdf.js so they
// can be unit-tested and shared without pulling the engine into any bundle.

export type PdfViewerEnv = {
  /** ``navigator.pdfViewerEnabled``; undefined where the browser does not report it. */
  pdfViewerEnabled: boolean | undefined;
  ios: boolean;
  coarsePointer: boolean;
};

export function currentPdfViewerEnv(): PdfViewerEnv {
  if (typeof navigator === 'undefined') return { pdfViewerEnabled: undefined, ios: false, coarsePointer: false };
  return {
    pdfViewerEnabled: typeof navigator.pdfViewerEnabled === 'boolean' ? navigator.pdfViewerEnabled : undefined,
    ios: isIosDevice(),
    coarsePointer: typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches,
  };
}

// Whether to draw the pages ourselves instead of framing the browser's built-in viewer. The built-in
// viewer is kept where it is a real, scrollable reader (desktop Chrome / Edge / Firefox / Safari, and
// the macOS / Windows desktop shells), because it brings search, selection and printing for free.
// It is replaced where framing it fails:
// - iOS / iPadOS WebKit paints only the first page of a framed PDF, as a static image that cannot be
//   scrolled, while still reporting a viewer;
// - Android Chrome (and other engines without one) report ``pdfViewerEnabled === false`` and show
//   nothing or a download prompt in the frame;
// - any other touch-first device, so phones and tablets all get the one reader that is built for
//   touch here (pinch zoom inside the sheet) and covered by the mobile browser tests.
export function shouldRenderPdfPages(env: PdfViewerEnv = currentPdfViewerEnv()): boolean {
  if (env.pdfViewerEnabled === false) return true;
  return env.ios || env.coarsePointer;
}

// Zoom is relative to "fit width" (1 = each page as wide as the viewer). Buttons walk this ladder;
// pinch and ctrl-wheel are continuous and clamped to the same range.
export const PDF_ZOOM_STEPS = [1, 1.25, 1.5, 2, 3, 4] as const;
export const PDF_MIN_ZOOM = PDF_ZOOM_STEPS[0];
export const PDF_MAX_ZOOM = PDF_ZOOM_STEPS[PDF_ZOOM_STEPS.length - 1];

export function clampPdfZoom(zoom: number): number {
  if (!Number.isFinite(zoom)) return PDF_MIN_ZOOM;
  return Math.min(PDF_MAX_ZOOM, Math.max(PDF_MIN_ZOOM, zoom));
}

/** The next ladder step strictly above (dir 1) or below (dir -1) ``zoom``, clamped to the range. */
export function stepPdfZoom(zoom: number, dir: 1 | -1): number {
  const eps = 0.001;
  if (dir > 0) return PDF_ZOOM_STEPS.find((s) => s > zoom + eps) ?? PDF_MAX_ZOOM;
  for (let i = PDF_ZOOM_STEPS.length - 1; i >= 0; i -= 1) if (PDF_ZOOM_STEPS[i] < zoom - eps) return PDF_ZOOM_STEPS[i];
  return PDF_MIN_ZOOM;
}

// Device pixels per CSS pixel for one page's canvas: the display's pixel ratio, reduced when the
// bitmap would exceed ``maxPixels``. iOS refuses canvases much past 16.7M pixels and keeps a small
// budget for all canvases together, so a zoomed-in page is drawn a little softer rather than blank.
export const PDF_MAX_CANVAS_PIXELS = 2 ** 23;

export function pdfOutputScale(cssWidth: number, cssHeight: number, devicePixelRatio: number, maxPixels = PDF_MAX_CANVAS_PIXELS): number {
  const ratio = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0 ? devicePixelRatio : 1;
  const area = cssWidth * cssHeight;
  if (!(area > 0)) return ratio;
  return Math.min(ratio, Math.sqrt(maxPixels / area));
}

/** Index of the page containing ``y`` given each page's top offset in ascending order (0 when empty). */
export function pageIndexAt(tops: readonly number[], y: number): number {
  let lo = 0;
  let hi = tops.length - 1;
  let found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (tops[mid] <= y) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}
