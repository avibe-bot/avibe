// Design frames never reach a server. Every request is answered here: from the
// frame's API fixtures (keyed by method and path plus query, method and path, path
// plus query, else path, so `POST /api/...` answers only that method; a fixture that
// is a `Response`, such as a file's raw text, is served as that response, and one
// that is `UNANSWERED` is never answered, as a request the server is still working
// on), else with an empty object. A WebSocket receives the messages its socket fixture lists (by path
// prefix) and otherwise stays connecting. An image the page points at an API path
// (agent media, `/api/media/<token>`) is drawn from the URL its fixture names, else as
// a blank pixel; an iframe a frame's page opens is drawn from the URL its fixture names
// (by path), else left blank. Paths a frame touched without a fixture are recorded, so
// the screenshot tool can report what a frame depends on.

/** An API fixture the server never answers: the request stays in flight, as a send does
 *  until the server has decided what to do with it. */
export const UNANSWERED = Symbol('unanswered');

/** A message a frame's WebSocket receives once open: text as is, `{ binary }` as UTF-8 bytes. */
export type DesignSocketMessage = string | { binary: string };

const touched = new Set<string>();
let responses: Record<string, unknown> = {};
let sockets: Record<string, DesignSocketMessage[]> = {};
let inflight = 0;

export const designNetwork = {
  setResponses(next: Record<string, unknown>) {
    responses = next;
  },
  setSockets(next: Record<string, DesignSocketMessage[]> = {}) {
    sockets = next;
  },
  unfixtured(): string[] {
    return [...touched].sort();
  },
  /** Resolves once no request has been in flight for two consecutive frames (at most `limitMs`). */
  async quiet(limitMs = 3000): Promise<void> {
    const deadline = performance.now() + limitMs;
    let calm = 0;
    while (calm < 2 && performance.now() < deadline) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      calm = inflight === 0 ? calm + 1 : 0;
    }
  },
};

function urlOf(input: RequestInfo | URL): URL {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(raw, window.location.href);
}

const BLANK_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

/** Where an image the page points at the API is drawn from: the URL its fixture names (a design asset). */
function imageSource(value: string): string {
  const url = urlOf(value);
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) return value;
  const key = [`${url.pathname}${url.search}`, url.pathname].find((candidate) => candidate in responses);
  if (key === undefined) touched.add(url.pathname);
  const body = key === undefined ? undefined : responses[key];
  return typeof body === 'string' ? body : BLANK_IMAGE;
}

/** Where an iframe the page opens is drawn from: the URL its fixture names (a design asset), else blank. */
function frameSource(value: string): string {
  const url = urlOf(value);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return value;
  const key = url.origin === window.location.origin ? url.pathname : undefined;
  const body = key === undefined ? undefined : responses[key];
  if (typeof body === 'string') return body;
  touched.add(url.origin === window.location.origin ? url.pathname : `${url.origin}${url.pathname}`);
  return 'about:blank';
}

/** Route every `src` an image (and, inside a frame, an iframe) is given through its source mapping. */
function installSources(frames: boolean) {
  const mapped = [
    [HTMLImageElement, imageSource],
    ...(frames ? [[HTMLIFrameElement, frameSource] as const] : []),
  ] as const;
  for (const [element, source] of mapped) {
    const src = Object.getOwnPropertyDescriptor(element.prototype, 'src')!;
    Object.defineProperty(element.prototype, 'src', {
      ...src,
      set(value: string) {
        src.set!.call(this, source(String(value)));
      },
    });
  }
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name: string, value: string) {
    const source = name.toLowerCase() === 'src' ? mapped.find(([element]) => this instanceof element)?.[1] : undefined;
    setAttribute.call(this, name, source ? source(String(value)) : value);
  };
}

export function installDesignNetwork() {
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    inflight += 1;
    try {
      const url = urlOf(input);
      const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
      const key = [
        `${method} ${url.pathname}${url.search}`,
        `${method} ${url.pathname}`,
        `${url.pathname}${url.search}`,
        url.pathname,
      ].find((candidate) => candidate in responses);
      if (key === undefined) touched.add(url.pathname);
      const body = key === undefined ? {} : responses[key];
      // Settled for the frame's quiet check at once: the page is waiting, not the shot.
      if (body === UNANSWERED) return new Promise<Response>(() => {});
      if (body instanceof Response) return body.clone();
      return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
    } finally {
      // Let the caller's continuation run before the request counts as settled.
      setTimeout(() => {
        inflight -= 1;
      }, 0);
    }
  };
  class SilentEventSource extends EventTarget {
    readonly url: string;
    readonly readyState = 0;
    readonly withCredentials = false;
    onopen = null;
    onmessage = null;
    onerror = null;
    constructor(url: string | URL) {
      super();
      this.url = String(url);
    }
    close() {}
  }
  window.EventSource = SilentEventSource as unknown as typeof EventSource;
  window.WebSocket = DesignWebSocket as unknown as typeof WebSocket;
  // The canvas frames each design in an iframe of its own; inside a frame, every iframe is the page's.
  installSources(new URLSearchParams(window.location.search).has('frame'));
}

/** A socket that opens and delivers its fixture's messages, or stays connecting when it has none. */
class DesignWebSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;
  readonly url: string;
  readonly protocol = '';
  readonly extensions = '';
  readonly bufferedAmount = 0;
  readyState = 0;
  binaryType: BinaryType = 'blob';
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  constructor(url: string | URL) {
    super();
    this.url = String(url);
    const path = urlOf(this.url).pathname;
    const key = Object.keys(sockets).filter((prefix) => path.startsWith(prefix)).sort((a, b) => b.length - a.length)[0];
    if (key === undefined) {
      touched.add(path);
      return;
    }
    inflight += 1;
    setTimeout(() => {
      this.readyState = 1;
      this.deliver('open', new Event('open'));
      for (const message of sockets[key]) {
        const data = typeof message === 'string' ? message : new TextEncoder().encode(message.binary).buffer;
        this.deliver('message', new MessageEvent('message', { data }));
      }
      setTimeout(() => {
        inflight -= 1;
      }, 0);
    }, 0);
  }
  private deliver(type: 'open' | 'message', event: Event) {
    this.dispatchEvent(event);
    if (type === 'open') this.onopen?.(event);
    else this.onmessage?.(event as MessageEvent);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
}
