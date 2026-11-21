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
/** The last list sent, and the rectangles of the last list the App said it follows. */
let sent = NOTHING;
let followed = new Map<string, NativeVoiceHoldSurface>();
let request = 0;
let scheduled = false;
let unlisten: (() => void) | null = null;

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

const measure = (): { list: NativeVoiceHoldSurface[]; holdMs: number } => {
  const list: NativeVoiceHoldSurface[] = [];
  let holdMs = 0;
  if (!coversViewport()) return { list, holdMs };
  for (const [id, surface] of surfaces) {
    // One threshold for the App: the first surface's. A surface that holds differently is the page's.
    if (holdMs && surface.holdMs !== holdMs) continue;
    const element = surface.element();
    if (!element || !element.isConnected) continue;
    const box = element.getBoundingClientRect();
    const rect = toWebView(box.left, box.top, box.width, box.height);
    if (!(rect.width > 0 && rect.height > 0)) continue;
    holdMs = surface.holdMs;
    list.push({ id, ...rect });
    if (list.length === MAX_SURFACES) break;
  }
  return { list, holdMs };
};

const send = (): void => {
  scheduled = false;
  if (!hasNativeVoiceHold()) return;
  const { list, holdMs } = measure();
  const next = JSON.stringify([list, holdMs]);
  if (next === sent) return;
  sent = next;
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

const listen = (): (() => void) => {
  const off = onNativeVoiceHold((event) => {
    const surface = event.surface ? surfaces.get(event.surface) : undefined;
    if (event.phase === 'recognised') {
      // Not a hold the page sees (the surface moved, the press already ended): the App drops it.
      if (!surface?.recognised(event.take)) void nativeVoiceHoldReject(event.take);
    } else if (event.phase === 'failed') {
      surface?.failed();
    }
  });
  const viewport = window.visualViewport;
  const meta = document.querySelector('meta[name="viewport"]');
  // The App's shell makes the document cover the viewport, or not, by rewriting this tag.
  const observer = meta && typeof MutationObserver === 'function' ? new MutationObserver(refreshHoldSurfaces) : null;
  if (meta) observer?.observe(meta, { attributes: true, attributeFilter: ['content'] });
  // The surfaces there are today are pinned (a composer, the tab bar): they move with the
  // viewport and the keyboard, not with a list that scrolls. A surface inside a scrolling
  // container would need that container's scroll here too.
  window.addEventListener('resize', refreshHoldSurfaces);
  viewport?.addEventListener('resize', refreshHoldSurfaces);
  viewport?.addEventListener('scroll', refreshHoldSurfaces);
  return () => {
    off?.();
    observer?.disconnect();
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
    refreshHoldSurfaces();
    if (surfaces.size === 0) {
      unlisten?.();
      unlisten = null;
    }
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

/** For tests: forgets every surface and what the App was told. */
export const resetHoldSurfacesForTest = (): void => {
  surfaces.clear();
  followed = new Map();
  sent = NOTHING;
  request = 0;
  scheduled = false;
  unlisten?.();
  unlisten = null;
};
