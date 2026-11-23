import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, Download, X, ZoomIn, ZoomOut } from 'lucide-react';
import { TransformWrapper, TransformComponent, type ReactZoomPanPinchRef } from 'react-zoom-pan-pinch';

import { Button } from '@/components/ui/button';
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from '@/components/ui/dialog';
import { handleMediaDownloadClick, mediaDownloadHref } from '@/lib/downloadMedia';
import { useRouteSurfaceActive, useRouteSurfaceWindowEvent } from '@/lib/routeSurfaceActivity';
import { cn } from '@/lib/utils';
import { readMotionMs, releaseVelocity, trackGesture, useMotionPreference, type GestureSample } from '@/lib/motion';
import { ImageViewerContext, type ImageViewerOpenOptions } from './image-viewer-context';

// A session-scoped image lightbox. ChatPage computes the ordered list of media-
// proxy image URLs in the transcript and wraps the page in a provider; any chat
// image (markdown inline image or a user attachment) opens it via ``open(src)``,
// and the lightbox pages left/right through the whole session's images. A caller
// whose images are not the transcript's — the composer's staged attachments, the
// queue strip's single file — names its own set on the open call instead (see
// ``ImageViewerOpenOptions``). Used through the optional context so the shared
// Markdown renderer keeps working (no-op) where there's no provider (e.g. the
// agent-config editor preview).

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
  //
  // The set this view pages through is recorded at open time rather than
  // re-derived per render, so it stays the set the opener named for the view's
  // whole life: the session gallery growing to include this src (the queue
  // flushing into the transcript, say) cannot hand a caller-owned view paging it
  // was opened without. ``null`` means the session gallery — the transcript's own
  // images, deliberately still live so streaming keeps extending it.
  const [view, setView] = React.useState<{ src: string; gallery: string[] | null } | null>(null);
  const src = view?.src ?? null;

  const open = React.useCallback(
    (next: string, options?: ImageViewerOpenOptions) =>
      setView({
        src: next,
        // One mechanism, three cases: an explicitly named set, the one-image set
        // ``isolated`` asks for, or the session gallery when the caller names none.
        gallery: options?.isolated === true ? [next] : options?.gallery ?? null,
      }),
    [],
  );
  const close = React.useCallback(() => setView(null), []);
  const routeSurfaceActive = useRouteSurfaceActive();
  // Controls the zoom/pan transform (wired to the desktop +/- buttons). The
  // wrapper is keyed on ``src`` so paging to another image remounts it back to 1x.
  const transformRef = React.useRef<ReactZoomPanPinchRef>(null);
  const previousFocusRef = React.useRef<HTMLElement | null>(null);
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

  const [gestureSurface, setGestureSurface] = React.useState<HTMLDivElement | null>(null);
  const backdropRef = React.useRef<HTMLDivElement>(null);
  const { reduced } = useMotionPreference();

  // The approved image-preview gesture (docs/plans/2026-11-17-ios-interactions.md).
  // Capture outside the zoom wrapper: its own touchmove otherwise pans first.
  // Never take a pinch, a zoomed pan, a control press, or an iOS edge gesture.
  React.useEffect(() => {
    const stage = stageRef.current;
    const backdrop = backdropRef.current;
    if (!gestureSurface || !stage || !backdrop || !src || !routeSurfaceActive) return;
    const chrome = gestureSurface.querySelectorAll<HTMLElement>('[data-viewer-chrome]');
    let gesture: { x: number; y: number; dy: number; axis: 'pending' | 'vertical' | 'yielded'; samples: GestureSample[] } | null = null;
    let exitTimer: ReturnType<typeof setTimeout> | undefined;
    const returnMs = readMotionMs('--motion-spring-snappy-duration') || 290;
    const chromeMs = readMotionMs('--motion-duration-fast') || 160;
    const easing = 'var(--motion-ease-move, cubic-bezier(0.32, 0.72, 0, 1))';
    const paint = (dy: number, returning = false) => {
      const progress = Math.min(1, Math.max(0, dy) / (window.innerHeight * 0.2));
      stage.style.transition = returning && !reduced ? `translate ${returnMs}ms ${easing}, scale ${returnMs}ms ${easing}` : 'none';
      stage.style.translate = `0px ${dy}px`;
      stage.style.scale = String(reduced ? 1 : 1 - 0.15 * progress);
      stage.style.opacity = '1';
      backdrop.style.transition = returning && !reduced ? `opacity ${returnMs}ms linear` : 'none';
      backdrop.style.opacity = String(1 - 0.85 * progress);
      chrome.forEach((node) => {
        node.style.transition = `opacity ${chromeMs}ms linear`;
        node.style.opacity = dy > 8 ? '0' : '1';
      });
    };
    const cancel = () => {
      gesture = null;
      paint(0, true);
    };
    const atFit = () => transformRef.current?.instance.state.scale === 1;
    const start = (event: TouchEvent) => {
      if (exitTimer !== undefined) return;
      if (event.touches.length !== 1) {
        // Once a second finger joins, this sequence belongs to pinch until all
        // fingers lift. Do not turn its final one-finger tail into a dismissal.
        interactedRef.current = true;
        cancel();
        return;
      }
      if (!atFit() || (event.target instanceof Element && event.target.closest('button, a'))) return;
      const point = event.touches[0];
      const edge = point.clientX <= 20 || point.clientX >= window.innerWidth - 20;
      gesture = { x: point.clientX, y: point.clientY, dy: 0, axis: edge ? 'yielded' : 'pending', samples: [{ t: event.timeStamp, at: point.clientY }] };
    };
    const move = (event: TouchEvent) => {
      if (!gesture) return;
      if (event.touches.length !== 1 || !atFit()) { cancel(); return; }
      const point = event.touches[0];
      const dx = point.clientX - gesture.x;
      const dy = point.clientY - gesture.y;
      if (Math.hypot(dx, dy) >= 6) interactedRef.current = true;
      if (gesture.axis === 'yielded') return;
      trackGesture(gesture.samples, { t: event.timeStamp, at: point.clientY });
      if (gesture.axis === 'pending') {
        if (Math.hypot(dx, dy) < 6) return;
        if (Math.abs(dy) <= Math.abs(dx) * 1.2) { gesture.axis = 'yielded'; return; }
        gesture.axis = 'vertical';
      }
      event.preventDefault();
      event.stopPropagation();
      gesture.dy = dy >= 0 ? dy : -24 * (1 - Math.exp(dy / 80));
      paint(gesture.dy);
    };
    const end = (event: TouchEvent) => {
      if (!gesture) return;
      const ended = gesture;
      gesture = null;
      const velocity = releaseVelocity(ended.samples, event.timeStamp);
      const dismiss = ended.axis === 'vertical' && ended.dy > 0 &&
        (ended.dy >= window.innerHeight * 0.2 || velocity >= 0.5);
      if (!dismiss) { paint(0, true); return; }
      const duration = reduced ? 140 : Math.min(320, Math.max(140, velocity > 0 ? (window.innerHeight - ended.dy) / velocity : 320));
      stage.style.transition = reduced ? `opacity ${duration}ms linear` : `translate ${duration}ms ${easing}, opacity ${duration}ms linear`;
      if (!reduced) stage.style.translate = `0px ${window.innerHeight}px`;
      stage.style.opacity = '0';
      backdrop.style.transition = `opacity ${duration}ms linear`;
      backdrop.style.opacity = '0';
      chrome.forEach((node) => { node.style.opacity = '0'; });
      exitTimer = setTimeout(close, duration);
    };
    paint(0);
    gestureSurface.addEventListener('touchstart', start, { capture: true, passive: true });
    gestureSurface.addEventListener('touchmove', move, { capture: true, passive: false });
    gestureSurface.addEventListener('touchend', end, true);
    gestureSurface.addEventListener('touchcancel', cancel, true);
    return () => {
      clearTimeout(exitTimer);
      paint(0);
      gestureSurface.removeEventListener('touchstart', start, true);
      gestureSurface.removeEventListener('touchmove', move, true);
      gestureSurface.removeEventListener('touchend', end, true);
      gestureSurface.removeEventListener('touchcancel', cancel, true);
    };
  }, [gestureSurface, src, routeSurfaceActive, reduced, close]);

  // Everything about paging reads from this one set: the arrows, the counter and
  // the keyboard handler below share the single condition it produces. A
  // one-image set (what ``isolated`` opens as) is therefore not pageable by any
  // of them, without a second rule that could drift from the first.
  const gallery = view?.gallery ?? images;
  const index = src ? gallery.indexOf(src) : -1;
  const pageable = index >= 0 && gallery.length > 1;
  const step = React.useCallback(
    (delta: number) => {
      if (!pageable) return;
      const next = gallery[(index + delta + gallery.length) % gallery.length];
      // Keep the view's own set: paging within a caller-owned gallery must not
      // quietly hand the next image back to the session gallery.
      setView((cur) => (cur ? { ...cur, src: next } : cur));
    },
    [index, gallery, pageable],
  );

  // The lightbox is a modal while its route surface is foreground: it OWNS
  // Escape / arrows. The activity-aware owner withdraws this capture listener
  // while the retained Chat route is hidden under Settings, but keeps `view`
  // and the rendered instance mounted for the return. Capture and immediate
  // propagation preserve the modal's ownership over lower global handlers
  // (notably the Composer's "Escape aborts recording" shortcut).
  useRouteSurfaceWindowEvent(
    'keydown',
    (e) => {
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
    },
    src !== null,
    true,
  );

  const ctx = React.useMemo(() => ({ open }), [open]);

  return (
    <Dialog
      open={src !== null && routeSurfaceActive}
      onOpenChange={(next) => { if (!next) close(); }}
    >
      <ImageViewerContext.Provider value={ctx}>
        {children}
        {src && (
          <DialogPortal forceMount>
            {/* The viewer is dark in every theme and says so, so the iOS status bar
                above it turns light. */}
            {routeSurfaceActive && <DialogOverlay ref={backdropRef} className="bg-black/80 backdrop-blur-sm scheme-dark" />}
            <DialogPrimitive.Content
              ref={setGestureSurface}
              forceMount
              aria-describedby={undefined}
              aria-hidden={!routeSurfaceActive || undefined}
              inert={!routeSurfaceActive || undefined}
              className={cn(
                'fixed inset-0 z-50 flex items-center justify-center p-6 scheme-dark focus:outline-none',
                !routeSurfaceActive && 'pointer-events-none invisible',
              )}
              onEscapeKeyDown={(event) => {
                if (!routeSurfaceActive) event.preventDefault();
              }}
              onOpenAutoFocus={() => {
                previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
              }}
              onCloseAutoFocus={(event) => {
                if (!routeSurfaceActive) {
                  event.preventDefault();
                  return;
                }
                const previousFocus = previousFocusRef.current;
                if (previousFocus?.isConnected) {
                  event.preventDefault();
                  previousFocus.focus();
                }
              }}
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
            >
              <DialogTitle className="sr-only">{t('chat.media.preview')}</DialogTitle>
          <div data-viewer-chrome className="absolute right-4 top-[calc(1rem+env(safe-area-inset-top))] z-10 flex items-center gap-2">
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
                data-viewer-chrome
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
                data-viewer-chrome
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
                  className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-elevation-3"
                />
              </TransformComponent>
            </TransformWrapper>
          </div>
          {pageable && (
            <span data-viewer-chrome className="absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-full bg-white/10 px-3 py-1 font-mono text-[11px] text-white">
              {index + 1} / {gallery.length}
            </span>
          )}
          </DialogPrimitive.Content>
        </DialogPortal>
      )}
      </ImageViewerContext.Provider>
    </Dialog>
  );
};
