// Per-frame record of the chat transcript's scroll, for the browser suite and
// the on-device lab alike. Each animation frame logs scrollTop, scrollHeight,
// and where a reference row sits in the viewport; every programmatic scroll of
// the transcript (a scrollTop write, scrollTo/scrollBy, or a row's
// scrollIntoView) is logged with the code that made it.
//
// The record observes the page; it changes nothing the product does.

export interface LabFrame {
  /** ms since recording started */
  t: number;
  top: number;
  height: number;
  client: number;
  /** The reference row's top, relative to the transcript's top edge (what the reader sees move). */
  ref: number | null;
  /** The reference row's top inside the scrolled content (moves only when content above it changes). */
  refInContent: number | null;
  /** Scroll events delivered since the previous frame. */
  scrolls: number;
  /** The reference row's id, and whether it was picked again this frame (``follow``). */
  id?: string | null;
  repinned?: boolean;
  /** The id of the first message row in the DOM, and how many rows there are. */
  first: string | null;
  rows: number;
  /** The older-page slot is showing its loading block. */
  loading: boolean;
}

export interface LabWrite {
  t: number;
  kind: 'scrollTop' | 'scrollTo' | 'scrollBy' | 'scrollIntoView';
  from: number;
  to: number;
  /** Raw stack frames (url:line:col) below the write, resolved to code by `resolveSites`. */
  stack: string[];
  site?: string;
}

export interface ScrollLab {
  /** ``follow``: whenever the reference row leaves the viewport, take the row
   *  under 40% of it instead (a reader scrolling through a whole conversation). */
  start(options?: { follow?: boolean }): void;
  stop(): void;
  clear(): void;
  frames: LabFrame[];
  writes: LabWrite[];
  /** Pick the row under the given fraction of the viewport height as the reference row;
   *  at 0, the first row that starts on screen. */
  pinRef(fraction?: number): string | null;
  refId: string | null;
  /** The reference row's current top relative to the transcript's top edge. */
  refTop(): number | null;
  resolveSites(): Promise<LabWrite[]>;
}

const TRANSCRIPT = '[data-testid="chat-transcript"]';
// Taken when this module loads, before a fixture stubs the network: resolving a
// write's code reads the page's own module source.
const nativeFetch = window.fetch.bind(window);
const transcript = () => document.querySelector<HTMLElement>(TRANSCRIPT);
const isTranscript = (node: unknown) => node instanceof HTMLElement && node.matches(TRANSCRIPT);

export function installScrollRecorder(): ScrollLab {
  let origin = performance.now();
  let recording = false;
  let scrolls = 0;
  const frames: LabFrame[] = [];
  const writes: LabWrite[] = [];

  // The frames below this recorder's own three (stackOf, log, and the patched
  // setter or method), whether it runs as its own module or inside a bundle.
  const stackOf = () =>
    (new Error().stack ?? '')
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => /:\d+:\d+\)?$/.test(line))
      .slice(3, 7);

  const log = (kind: LabWrite['kind'], el: HTMLElement, write: () => void) => {
    const from = el.scrollTop;
    write();
    if (recording) writes.push({ t: performance.now() - origin, kind, from, to: el.scrollTop, stack: stackOf() });
  };

  const scrollTop = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')!;
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    enumerable: scrollTop.enumerable,
    get() {
      return scrollTop.get!.call(this);
    },
    set(value: number) {
      if (!isTranscript(this)) return scrollTop.set!.call(this, value);
      log('scrollTop', this as HTMLElement, () => scrollTop.set!.call(this, value));
    },
  });
  for (const kind of ['scrollTo', 'scrollBy'] as const) {
    const original = Element.prototype[kind] as (...args: unknown[]) => void;
    Element.prototype[kind] = function (this: Element, ...args: unknown[]) {
      if (!isTranscript(this)) return original.apply(this, args);
      log(kind, this as HTMLElement, () => original.apply(this, args));
    } as typeof Element.prototype.scrollTo;
  }
  const scrollIntoView = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function (this: Element, ...args: Parameters<Element['scrollIntoView']>) {
    const el = transcript();
    if (!el || !el.contains(this)) return scrollIntoView.apply(this, args);
    log('scrollIntoView', el, () => scrollIntoView.apply(this, args));
  };

  document.addEventListener('scroll', (event) => { if (isTranscript(event.target)) scrolls += 1; }, { capture: true, passive: true });

  let follow = false;
  const sample = () => {
    const el = transcript();
    if (!el || !recording) return;
    const rows = el.querySelectorAll<HTMLElement>('[data-message-id]');
    let ref = lab.refId ? el.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(lab.refId)}"]`) : null;
    const box = el.getBoundingClientRect();
    let repinned = false;
    if (follow) {
      const rect = ref?.getBoundingClientRect();
      if (!rect || rect.bottom <= box.top || rect.top >= box.bottom) {
        lab.pinRef(0.4);
        ref = lab.refId ? el.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(lab.refId)}"]`) : null;
        repinned = true;
      }
    }
    const content = el.firstElementChild as HTMLElement | null;
    const refRect = ref?.getBoundingClientRect();
    frames.push({
      t: performance.now() - origin,
      top: el.scrollTop,
      height: el.scrollHeight,
      client: el.clientHeight,
      ref: refRect ? refRect.top - box.top : null,
      refInContent: refRect && content ? refRect.top - content.getBoundingClientRect().top : null,
      scrolls,
      ...(follow ? { id: lab.refId, repinned } : {}),
      first: rows[0]?.dataset.messageId ?? null,
      rows: rows.length,
      loading: !!el.querySelector('[role="status"]'),
    });
    scrolls = 0;
  };

  const lab: ScrollLab = {
    frames,
    writes,
    refId: null,
    start(options) {
      follow = options?.follow ?? false;
      origin = performance.now();
      frames.length = 0;
      writes.length = 0;
      scrolls = 0;
      if (recording) return;
      recording = true;
      // Sampled in a task posted from the frame callback, so it runs once the
      // frame's rendering steps (layout, ResizeObserver callbacks) are done: what
      // the frame painted, not the layout a frame callback sees before a resize
      // restore has run.
      const channel = new MessageChannel();
      channel.port1.onmessage = sample;
      const tick = () => {
        if (!recording) return;
        channel.port2.postMessage(null);
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    },
    stop() {
      recording = false;
    },
    clear() {
      frames.length = 0;
      writes.length = 0;
    },
    pinRef(fraction = 0.4) {
      const el = transcript();
      if (!el) return null;
      const box = el.getBoundingClientRect();
      const y = box.top + box.height * fraction;
      const rows = [...el.querySelectorAll<HTMLElement>('[data-message-id]')];
      const hit = fraction <= 0 ? rows.find((row) => row.getBoundingClientRect().top >= box.top) : rows.find((row) => {
        const rect = row.getBoundingClientRect();
        return rect.top <= y && rect.bottom > y;
      }) ?? rows.find((row) => row.getBoundingClientRect().top > y);
      lab.refId = hit?.dataset.messageId ?? null;
      return lab.refId;
    },
    refTop() {
      const el = transcript();
      const ref = el && lab.refId ? el.querySelector(`[data-message-id="${CSS.escape(lab.refId)}"]`) : null;
      return el && ref ? ref.getBoundingClientRect().top - el.getBoundingClientRect().top : null;
    },
    async resolveSites() {
      const sources = new Map<string, Promise<string[]>>();
      const linesOf = (url: string) => {
        if (!sources.has(url)) sources.set(url, nativeFetch(url).then((res) => res.text()).then((text) => text.split('\n')).catch(() => []));
        return sources.get(url)!;
      };
      const declaration = /(?:const|let)\s+(\w+)\s*=\s*(?:use(?:Callback|Memo)\(|\(|async\b|function\b)|function\s+(\w+)\s*\(|\b(use(?:Layout)?Effect)\(|new\s+(ResizeObserver)\(/;
      for (const write of writes) {
        const names: string[] = [];
        for (const frame of write.stack) {
          const match = /(https?:\/\/[^\s()]+):(\d+):\d+\)?$/.exec(frame);
          // The page's own code: dev-server modules, or a built page's assets.
          if (!match || !/\/(src|assets)\//.test(match[1])) continue;
          const lines = await linesOf(match[1]);
          let name = '?';
          for (let line = Number(match[2]) - 1; line >= 0; line -= 1) {
            const found = declaration.exec(lines[line] ?? '');
            if (found) {
              name = found[1] ?? found[2] ?? found[3] ?? found[4];
              break;
            }
          }
          const file = match[1].replace(/\?.*$/, '').split('/').pop();
          names.push(`${name}@${file}`);
        }
        write.site = names.slice(0, 2).join(' < ') || 'browser';
      }
      return writes;
    },
  };
  return lab;
}
