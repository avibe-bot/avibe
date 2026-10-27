import { isSafeDocumentLink } from '@/lib/filePreview';
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

// ── Links ─────────────────────────────────────────────────────────────────────
// The fields of a pdf.js link annotation the reader uses (``page.getAnnotations()`` data).
export type PdfLinkAnnotation = {
  subtype?: string;
  annotationFlags?: number;
  rect?: ArrayLike<number> | null;
  quadPoints?: ArrayLike<number> | null;
  /** A link pdf.js validated as absolute (http, https, ftp, mailto, tel). */
  url?: string;
  /** A named destination (string) or an explicit one ([page, {name}, ...args]). */
  dest?: unknown;
};

export type PdfLinkTarget = { kind: 'url'; href: string } | { kind: 'dest'; dest: string | readonly unknown[] };
export type PdfRect = [number, number, number, number];

const HIDDEN_FLAG = 0x2;

/**
 * Where a link annotation goes: a web or mail link to open in a new tab, or a destination inside
 * the document. Anything else — scripts, launch and file actions, non-web schemes, hidden links —
 * is not offered at all.
 */
export function pdfLinkTarget(link: PdfLinkAnnotation, base: string): PdfLinkTarget | null {
  if (link.subtype !== 'Link' || ((link.annotationFlags ?? 0) & HIDDEN_FLAG) !== 0) return null;
  if (typeof link.url === 'string' && link.url) return isSafeDocumentLink(link.url, base) ? { kind: 'url', href: link.url } : null;
  if (typeof link.dest === 'string' && link.dest) return { kind: 'dest', dest: link.dest };
  if (Array.isArray(link.dest) && link.dest.length > 0) return { kind: 'dest', dest: link.dest };
  return null;
}

/** The clickable areas of a link, in PDF user space: one per quadrilateral, or else its rectangle. */
export function pdfLinkAreas(link: PdfLinkAnnotation): PdfRect[] {
  const quads = link.quadPoints;
  if (quads && quads.length >= 8 && quads.length % 8 === 0) {
    const areas: PdfRect[] = [];
    for (let i = 0; i < quads.length; i += 8) {
      const xs = [quads[i], quads[i + 2], quads[i + 4], quads[i + 6]];
      const ys = [quads[i + 1], quads[i + 3], quads[i + 5], quads[i + 7]];
      areas.push([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
    }
    return areas;
  }
  const r = link.rect;
  if (!r || r.length !== 4) return [];
  return [[Math.min(r[0], r[2]), Math.min(r[1], r[3]), Math.max(r[0], r[2]), Math.max(r[1], r[3])]];
}

/**
 * The point an explicit destination asks to show, in PDF user space; ``null`` where the destination
 * leaves that coordinate unchanged (or names no point at all, as Fit does).
 */
export function pdfDestPoint(dest: readonly unknown[]): { x: number | null; y: number | null } {
  const kind = (dest[1] as { name?: unknown } | null | undefined)?.name;
  const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
  switch (kind) {
    case 'XYZ':
      return { x: num(dest[2]), y: num(dest[3]) };
    case 'FitH':
    case 'FitBH':
      return { x: null, y: num(dest[2]) };
    case 'FitV':
    case 'FitBV':
      return { x: num(dest[2]), y: null };
    case 'FitR':
      return { x: num(dest[2]), y: num(dest[5]) };
    default:
      return { x: null, y: null };
  }
}
