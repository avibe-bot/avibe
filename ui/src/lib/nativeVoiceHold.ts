// The iOS App recognises a hold itself (ios/Avibe/NativeHoldCapture.swift): the page names the
// places where a hold may record now, and the App times each touch there, gives the cue at the
// threshold and has the microphone open from touch-down. The page then takes that take, or
// refuses it. This file keeps the list the App has; the gesture stays components/voice/useHoldToTalk.
//
// The page's own recognition remains the fallback: a place the App does not follow (an older App,
// a popup, a document that is not `viewport-fit=cover`, a list not yet answered) holds as before.
import { appVoiceEnabled, subscribeAppVoice } from './appVoice';
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
  /** Whether a touch here is to open no microphone: the App then starts recording when it
   *  recognises the hold. Asked at each measure; unsaid, the App's default (at touch-down). */
  startsAtRecognition?: () => boolean;
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

/** The App takes at most this many boxes. */
const MAX_BOXES = 8;
/** A surface's own box, and at most this many of its children's that stick out of it. */
const MAX_PARTS = 2;

const surfaces = new Map<string, HoldSurface>();
/** What the App has before the page names anything: no surface. */
const NOTHING = JSON.stringify([[], 0]);
/** The last list sent (as text, and each surface's boxes), and the boxes of the last list the
 *  App said it follows. */
let sent = NOTHING;
let named = new Map<string, NativeVoiceHoldSurface[]>();
let followed: NativeVoiceHoldSurface[] = [];
let request = 0;
let scheduled = false;
/** Measuring once more: after a scroll has stopped, and after the page has come to rest. */
let afterScroll: ReturnType<typeof setTimeout> | null = null;
let atRest: ReturnType<typeof setTimeout> | null = null;
/** The measure now due is the one made at rest: it asks for no further one. */
let resting = false;
let unlisten: (() => void) | null = null;

/** How long after a scroll, or after something was put over the page or taken off it (a sheet
 *  rising, a dialog closing, the slide to another page), the surfaces are measured once more:
 *  where they came to rest. Longer than the longest of those motions, the 260 ms slide to
 *  another page (`--motion-nav-push-duration`), with room to spare. */
const SETTLE_MS = 400;

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

/** A surface's box in the App's list: its own under the surface's id, a part's under `id_1`… */
const partId = (id: string, part: number): string => (part === 0 ? id : `${id}_${part}`);
const surfaceOf = (boxId: string): string => boxId.replace(/_\d+$/, '');

/** Whether a touch in the element's box reaches it: it is what lies on top at its centre, and
 *  nothing has made it inert. A surface under a sheet, a dialog or a viewer is not one: the App
 *  acts on a touch before the page sees it, and would open the microphone for whatever covers it. */
const reachable = (element: Element, box: DOMRect): boolean => {
  if (element.closest('[inert]')) return false;
  const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  return Boolean(top) && element.contains(top);
};

/** The element's box, and those of its children that stick out of it (the main Bot's face is
 *  lifted above its button): a touch on any of them is the element's. Not where the element
 *  clips what leaves its box (a scrolling input): nothing of a child is to be touched there. */
const boxesOf = (element: Element, box: DOMRect): DOMRect[] => {
  const boxes = [box];
  const { overflowX, overflowY } = getComputedStyle(element);
  if (overflowX !== 'visible' || overflowY !== 'visible') return boxes;
  for (const child of element.children) {
    if (boxes.length > MAX_PARTS) break;
    const part = child.getBoundingClientRect();
    const inside = part.left >= box.left && part.right <= box.right && part.top >= box.top && part.bottom <= box.bottom;
    if (part.width > 0 && part.height > 0 && !inside) boxes.push(part);
  }
  return boxes;
};

/** The list for the App, and whether a surface was left out only because something lies over
 *  it now (which may be passing: a page sliding in, a card fading out). */
const measure = (): { list: NativeVoiceHoldSurface[]; holdMs: number; covered: boolean } => {
  const list: NativeVoiceHoldSurface[] = [];
  let holdMs = 0;
  let covered = false;
  // Turned off for this instance (lib/appVoice): the App is told it follows nothing.
  if (!appVoiceEnabled() || !coversViewport()) return { list, holdMs, covered };
  for (const [id, surface] of surfaces) {
    // One threshold for the App: the first surface's. A surface that holds differently is the page's.
    if (holdMs && surface.holdMs !== holdMs) continue;
    let boxes: NativeVoiceHoldSurface[] | undefined;
    if (surface.live()) {
      // Its own card may lie over it now; it is the App's as last named until the press is over.
      boxes = named.get(id);
    } else {
      const element = surface.element();
      if (!element || !element.isConnected) continue;
      const box = element.getBoundingClientRect();
      if (!(box.width > 0 && box.height > 0)) continue;
      if (!reachable(element, box)) {
        covered = true;
        continue;
      }
      const start = surface.startsAtRecognition?.() ? { start: 'recognition' as const } : {};
      boxes = boxesOf(element, box).map((part, index) => ({
        id: partId(id, index),
        ...toWebView(part.left, part.top, part.width, part.height),
        ...start,
      }));
    }
    if (!boxes || boxes.length === 0 || list.length + boxes.length > MAX_BOXES) continue;
    holdMs = surface.holdMs;
    list.push(...boxes);
  }
  return { list, holdMs, covered };
};

const send = (): void => {
  scheduled = false;
  const rested = resting;
  resting = false;
  if (hasNativeVoiceHold() && tell() && !rested) measureAtRest();
  // The last surface went: the App has been told, and has nothing more to say.
  if (surfaces.size === 0) {
    unlisten?.();
    unlisten = null;
  }
};

/** Tells the App the list if it changed. True when a surface was left out as covered. */
const tell = (): boolean => {
  const { list, holdMs, covered } = measure();
  const next = JSON.stringify([list, holdMs]);
  if (next === sent) return covered;
  sent = next;
  named = new Map();
  for (const box of list) named.set(surfaceOf(box.id), [...(named.get(surfaceOf(box.id)) ?? []), box]);
  request += 1;
  const mine = request;
  void nativeVoiceHoldSurfaces(list, holdMs).then((answer) => {
    // A list overtaken by a later one says nothing: that one's answer does.
    if (mine !== request || answer === 'superseded') return;
    followed = answer === 'ok' ? list : [];
  });
  return covered;
};

/** Measures the surfaces again, soon, and tells the App if they changed. */
export const refreshHoldSurfaces = (): void => {
  if (scheduled || (surfaces.size === 0 && sent === NOTHING)) return;
  scheduled = true;
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(send);
  else setTimeout(send, 0);
};

/** Measures once more when what is moving has come to rest. One such measure is waited for at
 *  a time, and it asks for no further one: a surface that stays covered is not polled. */
const measureAtRest = (): void => {
  if (atRest) return;
  atRest = setTimeout(() => {
    atRest = null;
    resting = true;
    refreshHoldSurfaces();
  }, SETTLE_MS);
};

/** Measures again now, and once more when what moved has come to rest. */
const refreshAndSettle = (): void => {
  refreshHoldSurfaces();
  if (atRest) clearTimeout(atRest);
  atRest = null;
  measureAtRest();
};

/** Measures once a scroll has stopped: a surface that scrolls with its page (a home's composer)
 *  is where it was named until then, and holds through the page meanwhile. */
const refreshAfterScroll = (): void => {
  if (afterScroll) clearTimeout(afterScroll);
  afterScroll = setTimeout(() => {
    afterScroll = null;
    refreshHoldSurfaces();
  }, SETTLE_MS);
};

const listen = (): (() => void) => {
  const off = onNativeVoiceHold((event) => {
    const surface = event.surface ? surfaces.get(surfaceOf(event.surface)) : undefined;
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
  const unsubscribe = subscribeAppVoice(refreshHoldSurfaces);
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
    unsubscribe();
    observer?.disconnect();
    overlays?.disconnect();
    if (afterScroll) clearTimeout(afterScroll);
    if (atRest) clearTimeout(atRest);
    afterScroll = null;
    atRest = null;
    window.removeEventListener('pointerdown', refreshHoldSurfaces, { capture: true });
    window.removeEventListener('scroll', refreshAfterScroll, { capture: true });
    window.removeEventListener('resize', refreshHoldSurfaces);
    viewport?.removeEventListener('resize', refreshHoldSurfaces);
    viewport?.removeEventListener('scroll', refreshHoldSurfaces);
  };
};

/** Adds a surface under `id` (letters, digits and `-`); the returned function takes it back. */
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
  const point = toWebView(clientX, clientY, 0, 0);
  return followed.some((box) => (
    surfaceOf(box.id) === id
    && point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height
  ));
};
