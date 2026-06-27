import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, Download, X, ZoomIn, ZoomOut } from 'lucide-react';
import { TransformWrapper, TransformComponent, type ReactZoomPanPinchRef } from 'react-zoom-pan-pinch';

import { Button } from '@/components/ui/button';
import { handleMediaDownloadClick, mediaDownloadHref } from '@/lib/downloadMedia';

// A session-scoped image lightbox. ChatPage computes the ordered list of media-
// proxy image URLs in the transcript and wraps the page in a provider; any chat
// image (markdown inline image or a user attachment) opens it via ``open(src)``,
// and the lightbox pages left/right through the whole session's images. Used
// through the optional context so the shared Markdown renderer keeps working
// (no-op) where there's no provider (e.g. the agent-config editor preview).

type ImageViewerContextValue = { open: (src: string) => void };

const ImageViewerContext = React.createContext<ImageViewerContextValue | null>(null);

export function useImageViewer(): ImageViewerContextValue | null {
  return React.useContext(ImageViewerContext);
}

// Overlay controls sit on a dark backdrop, so the shared Button's themed
// foreground/hover (tuned for app surfaces) would be invisible here — override
// to white-on-translucent while still inheriting Button's sizing/focus/disabled
// behavior instead of hand-rolling a <button>.
const OVERLAY_BTN = 'bg-white/10 text-white hover:bg-white/20 hover:text-white';

export const ImageViewerProvider: React.FC<{ images: string[]; children: React.ReactNode }> = ({
  images,
  children,
}) => {
  const { t } = useTranslation();
  // Track the *displayed URL*, not an index. ``images`` is recomputed on every
  // streamed message, so a stored index would drift; and keeping the context
  // value (``open``) free of any ``images`` dependency means it stays stable, so
  // chat images don't re-render on every streaming tick. A clicked src that
  // isn't in the list (shouldn't happen for our own clean proxy URLs, but be
  // safe) still shows exactly what was clicked — paging just turns off for it.
  const [src, setSrc] = React.useState<string | null>(null);

  const open = React.useCallback((next: string) => setSrc(next), []);
  const close = React.useCallback(() => setSrc(null), []);
  // Controls the zoom/pan transform (wired to the desktop +/- buttons). The
  // wrapper is keyed on ``src`` so paging to another image remounts it back to 1x.
  const transformRef = React.useRef<ReactZoomPanPinchRef>(null);
  // True while/after a pan in the current press, so the trailing click (which may
  // land on the dark stage after dragging a zoomed image) doesn't close the modal.
  // Reset on each pointer-down; clicking the empty stage WITHOUT panning closes.
  const interactedRef = React.useRef(false);
  // The <img> has pointer-events:none under react-zoom-pan-pinch (the wrapper owns
  // gestures), so we can't tell "clicked the image" from "clicked the empty stage"
  // by event target — decide by hit-testing the image's rect in the backdrop click.
  const imgRef = React.useRef<HTMLImageElement>(null);
  // The 90vw×90vh zoom stage. A zoomed image's rect overflows it (clipped), so the
  // close hit-test must also require the click to be inside the visible stage.
  const stageRef = React.useRef<HTMLDivElement>(null);

  const index = src ? images.indexOf(src) : -1;
  const pageable = index >= 0 && images.length > 1;
  const step = React.useCallback(
    (delta: number) => {
      if (index < 0 || images.length === 0) return;
      setSrc(images[(index + delta + images.length) % images.length]);
    },
    [index, images],
  );

  React.useEffect(() => {
    if (src === null) return;
    // The lightbox is a modal: while open it OWNS Escape / arrows. Listen in the
    // capture phase and stop immediate propagation on the keys we handle so a
    // lower global handler (notably the Composer's "Escape aborts recording")
    // can't also fire — Escape here must only close the viewer, not discard an
    // in-progress voice recording. Capture runs before any bubble-phase window
    // listener regardless of registration order, so ownership is deterministic.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopImmediatePropagation();
        close();
      } else if (e.key === 'ArrowLeft') {
        e.stopImmediatePropagation();
        step(-1);
      } else if (e.key === 'ArrowRight') {
        e.stopImmediatePropagation();
        step(1);
      }
    };
    window.addEventListener('keydown', onKey, { capture: true });
    return () => window.removeEventListener('keydown', onKey, { capture: true });
  }, [src, close, step]);

  const ctx = React.useMemo(() => ({ open }), [open]);

  return (
    <ImageViewerContext.Provider value={ctx}>
      {children}
      {src && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6 backdrop-blur-sm"
          onPointerDown={() => {
            interactedRef.current = false;
          }}
          onClick={(e) => {
            // A real pan happened this press → don't treat the release as a close.
            if (interactedRef.current) return;
            // Keep the viewer open only when the click is on the VISIBLE image:
            // inside the image rect AND inside the stage. (A zoomed image's rect
            // overflows the stage; those clipped parts are dark margin → close.)
            const inside = (rect?: DOMRect) =>
              !!rect &&
              e.clientX >= rect.left &&
              e.clientX <= rect.right &&
              e.clientY >= rect.top &&
              e.clientY <= rect.bottom;
            if (
              inside(imgRef.current?.getBoundingClientRect()) &&
              inside(stageRef.current?.getBoundingClientRect())
            ) {
              return;
            }
            close();
          }}
          role="dialog"
          aria-modal="true"
        >
          <div className="absolute right-4 top-4 z-10 flex items-center gap-2">
            {/* Zoom buttons: a pointer-device affordance (touch zooms by pinch).
                stopPropagation so clicking a control doesn't also close the viewer. */}
            <Button
              variant="ghost"
              size="icon"
              onClick={(e) => {
                e.stopPropagation();
                transformRef.current?.zoomOut();
              }}
              aria-label={t('chat.viewer.zoomOut')}
              className={`hidden sm:inline-flex ${OVERLAY_BTN}`}
            >
              <ZoomOut className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={(e) => {
                e.stopPropagation();
                transformRef.current?.zoomIn();
              }}
              aria-label={t('chat.viewer.zoomIn')}
              className={`hidden sm:inline-flex ${OVERLAY_BTN}`}
            >
              <ZoomIn className="size-4" />
            </Button>
            <Button
              asChild
              variant="ghost"
              size="icon"
              className={OVERLAY_BTN}
            >
              <a
                href={mediaDownloadHref(src)}
                download
                onClick={(e) => {
                  e.stopPropagation();
                  handleMediaDownloadClick(e, src);
                }}
                aria-label={t('chat.media.download')}
              >
                <Download className="size-4" />
              </a>
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={close}
              aria-label={t('chat.viewer.close')}
              className={OVERLAY_BTN}
            >
              <X className="size-4" />
            </Button>
          </div>
          {pageable && (
            <>
              <Button
                variant="ghost"
                size="icon"
                onClick={(e) => {
                  e.stopPropagation();
                  step(-1);
                }}
                aria-label={t('chat.viewer.previous')}
                className={`absolute left-4 top-1/2 z-10 -translate-y-1/2 rounded-full ${OVERLAY_BTN}`}
              >
                <ChevronLeft className="size-5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                onClick={(e) => {
                  e.stopPropagation();
                  step(1);
                }}
                aria-label={t('chat.viewer.next')}
                className={`absolute right-4 top-1/2 z-10 -translate-y-1/2 rounded-full ${OVERLAY_BTN}`}
              >
                <ChevronRight className="size-5" />
              </Button>
            </>
          )}
          {/* Zoom/pan the image IN-COMPONENT: the app viewport disables native
              pinch-zoom (user-scalable=no, for the iOS keyboard fix), so the
              lightbox provides its own. Mobile: pinch to zoom, drag to pan, double
              tap to toggle. Desktop: wheel to zoom, drag to pan, double-click /
              the +/- buttons. Keyed on src so paging resets to a fit view. Closing
              by click is decided in the backdrop handler (hit-test the image rect +
              the pan flag), since the library makes the <img> pointer-events:none. */}
          <div ref={stageRef}>
            <TransformWrapper
              key={src}
              ref={transformRef}
              initialScale={1}
              minScale={1}
              maxScale={5}
              centerOnInit
              doubleClick={{ mode: 'toggle' }}
              onPanning={() => {
                // Set only on ACTUAL movement (not pan-start, which fires on
                // pointer-down), so a clean tap on the empty stage still closes.
                interactedRef.current = true;
              }}
            >
              {/* The zoom viewport is the whole 90vw×90vh stage, NOT shrunk to the
                  image box (the #681 bug: a wide-but-short image was boxed in its
                  letterbox strip, so zooming couldn't grow it into the free height).
                  The content auto-sizes to the image and centerOnInit centers it in
                  the stage; zooming scales the image into the full area. */}
              <TransformComponent wrapperStyle={{ width: '90vw', height: '90vh', cursor: 'grab' }}>
                <img
                  ref={imgRef}
                  src={src}
                  alt=""
                  className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
                />
              </TransformComponent>
            </TransformWrapper>
          </div>
          {pageable && (
            <span className="absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-full bg-white/10 px-3 py-1 font-mono text-[11px] text-white">
              {index + 1} / {images.length}
            </span>
          )}
        </div>
      )}
    </ImageViewerContext.Provider>
  );
};
