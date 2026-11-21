// The iOS App recognises a hold itself (ios/Avibe/NativeHoldCapture.swift): the page names the
// places where a hold may record now, and the App times each touch there, gives the cue at the
// threshold and has the microphone open from touch-down. The page then takes that take, or
// refuses it. This file keeps the list the App has; the gesture stays components/voice/useHoldToTalk.
//
// The page's own recognition remains the fallback: a place the App does not follow (an older App,
// a popup, a document that is not `viewport-fit=cover`, a list not yet answered) holds as before.
import {
  hasNativeVoiceHold,
  nativeVoiceHoldReject,
  nativeVoiceHoldSurfaces,
  onNativeVoiceHold,
  type NativeVoiceHoldSurface,
} from './native';

export type HoldSurface = {
  /** The hold threshold of a finger here, in ms. */
  holdMs: number;
  /** The element a hold may start on now: every touch in its box is this surface's. Null when
   *  none may record now (the App then follows no press here). */
  element: () => Element | null;
  /** A press or a take is live here: the surface stays as the App last had it, to its end
   *  (taking it back under a finger would end the press the App follows). */
  live: () => boolean;
  /** The App recognised a hold here and gave the cue: true when this surface takes its take. */
  recognised: (take: string) => boolean;
  /** The App could not open the microphone for the press it follows here. */
  failed: () => void;
};

/** How long past the threshold a press the App follows waits for the App's word before the page
 *  recognises the hold itself. The App's event normally arrives before the page's own timer. */
export const NATIVE_HOLD_GRACE_MS = 100;

/** The App takes at most this many surfaces. */
const MAX_SURFACES = 8;

const surfaces = new Map<string, HoldSurface>();
/** What the App has before the page names anything: no surface. */
const NOTHING = JSON.stringify([[], 0]);
/** The last list sent (as text, and by surface), and the rectangles of the last list the App
 *  said it follows. */
let sent = NOTHING;
let named = new Map<string, NativeVoiceHoldSurface>();
let followed = new Map<string, NativeVoiceHoldSurface>();
let request = 0;
let scheduled = false;
let later: ReturnType<typeof setTimeout> | null = null;
let unlisten: (() => void) | null = null;

/** How long after a scroll, or after something was put over the page or taken off it (a sheet
 *  rising, a dialog closing), the surfaces are measured once more: where they came to rest. */
const SETTLE_MS = 300;

/** The WebView's own points for a box of the layout viewport: the App compares them with the
 *  touch's place in the WebView. Only a `viewport-fit=cover` document starts at the WebView's
 *  corner; another is inset by the system, by an amount the page cannot read. */
const coversViewport = (): boolean => {
  const content = document.querySelector('meta[name="viewport"]')?.getAttribute('content') ?? '';
  return content.split(/[,;]/).some((entry) => {
    const [name, value] = entry.split('=').map((part) => part.trim().toLowerCase());
    return name === 'viewport-fit' && value === 'cover';
  });
};

const half = (value: number): number => Math.round(value * 2) / 2;

const toWebView = (left: number, top: number, width: number, height: number) => {
  const viewport = window.visualViewport;
  const scale = viewport?.scale ?? 1;
  return {
    x: half((left - (viewport?.offsetLeft ?? 0)) * scale),
    y: half((top - (viewport?.offsetTop ?? 0)) * scale),
    width: half(width * scale),
    height: half(height * scale),
  };
};

/** Whether a touch in the element's box reaches it: it is what lies on top at its centre, and
 *  nothing has made it inert. A surface under a sheet, a dialog or a viewer is not one: the App
 *  acts on a touch before the page sees it, and would open the microphone for whatever covers it. */
const reachable = (element: Element, box: DOMRect): boolean => {
  if (element.closest('[inert]')) return false;
  const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  return Boolean(top) && element.contains(top);
};

const measure = (): { list: NativeVoiceHoldSurface[]; holdMs: number } => {
  const list: NativeVoiceHoldSurface[] = [];
  let holdMs = 0;
  if (!coversViewport()) return { list, holdMs };
  for (const [id, surface] of surfaces) {
    // One threshold for the App: the first surface's. A surface that holds differently is the page's.
    if (holdMs && surface.holdMs !== holdMs) continue;
    let rect: NativeVoiceHoldSurface | undefined;
    if (surface.live()) {
      // Its own card may lie over it now; it is the App's as last named until the press is over.
      rect = named.get(id);
    } else {
      const element = surface.element();
      if (!element || !element.isConnected) continue;
      const box = element.getBoundingClientRect();
      if (!(box.width > 0 && box.height > 0) || !reachable(element, box)) continue;
      rect = { id, ...toWebView(box.left, box.top, box.width, box.height) };
    }
    if (!rect) continue;
    holdMs = surface.holdMs;
    list.push(rect);
    if (list.length === MAX_SURFACES) break;
  }
  return { list, holdMs };
};

const send = (): void => {
  scheduled = false;
  if (hasNativeVoiceHold()) tell();
  // The last surface went: the App has been told, and has nothing more to say.
  if (surfaces.size === 0) {
    unlisten?.();
    unlisten = null;
  }
};

const tell = (): void => {
  const { list, holdMs } = measure();
  const next = JSON.stringify([list, holdMs]);
  if (next === sent) return;
  sent = next;
  named = new Map(list.map((surface) => [surface.id, surface]));
  request += 1;
  const mine = request;
  void nativeVoiceHoldSurfaces(list, holdMs).then((answer) => {
    // A list overtaken by a later one says nothing: that one's answer does.
    if (mine !== request || answer === 'superseded') return;
    followed = new Map(answer === 'ok' ? list.map((surface) => [surface.id, surface]) : []);
  });
};

/** Measures the surfaces again, soon, and tells the App if they changed. */
export const refreshHoldSurfaces = (): void => {
  if (scheduled || (surfaces.size === 0 && sent === NOTHING)) return;
  scheduled = true;
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(send);
  else setTimeout(send, 0);
};

/** Measures again now, and once more when what moved has come to rest. */
const refreshAndSettle = (): void => {
  refreshHoldSurfaces();
  if (later) clearTimeout(later);
  later = setTimeout(() => {
    later = null;
    refreshHoldSurfaces();
  }, SETTLE_MS);
};

/** Measures once a scroll has stopped: a surface that scrolls with its page (a home's composer)
 *  is where it was named until then, and holds through the page meanwhile. */
const refreshAfterScroll = (): void => {
  if (later) clearTimeout(later);
  later = setTimeout(() => {
    later = null;
    refreshHoldSurfaces();
  }, SETTLE_MS);
};

const listen = (): (() => void) => {
  const off = onNativeVoiceHold((event) => {
    const surface = event.surface ? surfaces.get(event.surface) : undefined;
    if (event.phase === 'recognised') {
      // Not a hold the page sees (the surface moved, the press already ended): the App drops it,
      // and the surfaces are measured again in case it was the list that was wrong.
      if (!surface?.recognised(event.take)) {
        void nativeVoiceHoldReject(event.take);
        refreshHoldSurfaces();
      }
    } else if (event.phase === 'failed') {
      surface?.failed();
    }
  });
  const viewport = window.visualViewport;
  const meta = document.querySelector('meta[name="viewport"]');
  // The App's shell makes the document cover the viewport, or not, by rewriting this tag.
  const observer = typeof MutationObserver === 'function' ? new MutationObserver(refreshHoldSurfaces) : null;
  if (meta) observer?.observe(meta, { attributes: true, attributeFilter: ['content'] });
  // A sheet, a dialog or a viewer is put on the page, or taken off it, as a child of the body.
  const overlays = typeof MutationObserver === 'function' ? new MutationObserver(refreshAndSettle) : null;
  overlays?.observe(document.body, { childList: true });
  // Any touch: a list that had gone stale is right again for the next one.
  window.addEventListener('pointerdown', refreshHoldSurfaces, { capture: true, passive: true });
  window.addEventListener('scroll', refreshAfterScroll, { capture: true, passive: true });
  window.addEventListener('resize', refreshHoldSurfaces);
  viewport?.addEventListener('resize', refreshHoldSurfaces);
  viewport?.addEventListener('scroll', refreshHoldSurfaces);
  return () => {
    off?.();
    observer?.disconnect();
    overlays?.disconnect();
    if (later) clearTimeout(later);
    later = null;
    window.removeEventListener('pointerdown', refreshHoldSurfaces, { capture: true });
    window.removeEventListener('scroll', refreshAfterScroll, { capture: true });
    window.removeEventListener('resize', refreshHoldSurfaces);
    viewport?.removeEventListener('resize', refreshHoldSurfaces);
    viewport?.removeEventListener('scroll', refreshHoldSurfaces);
  };
};

/** Adds a surface under `id` (letters, digits, `-` and `_`); the returned function takes it back. */
export const registerHoldSurface = (id: string, surface: HoldSurface): (() => void) => {
  if (!hasNativeVoiceHold()) return () => undefined;
  surfaces.set(id, surface);
  unlisten ??= listen();
  refreshHoldSurfaces();
  return () => {
    if (surfaces.get(id) !== surface) return;
    surfaces.delete(id);
    if (surfaces.size === 0 && sent === NOTHING) {
      // Nothing was ever named: the App has nothing to say.
      unlisten?.();
      unlisten = null;
      return;
    }
    // What listens to the App stays until it has been told (a word on its way is still refused).
    refreshHoldSurfaces();
  };
};

/** Whether the App follows a press at this point of the layout viewport on surface `id`: it said
 *  so for a box that holds the point. The page then waits `NATIVE_HOLD_GRACE_MS` for its word. */
export const holdFollowed = (id: string, clientX: number, clientY: number): boolean => {
  const box = followed.get(id);
  if (!box) return false;
  const point = toWebView(clientX, clientY, 0, 0);
  return point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height;
};
