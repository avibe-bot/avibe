// Design frames never reach a server. Every request is answered here: from the
// frame's API fixtures (keyed by path plus query, else by path), else with an empty
// object. An image the page points at an API path (agent media, `/api/media/<token>`)
// is drawn from the URL its fixture names, else as a blank pixel. Paths a frame
// touched without a fixture are recorded, so the screenshot tool can report what a
// frame depends on.

const touched = new Set<string>();
let responses: Record<string, unknown> = {};
let inflight = 0;

export const designNetwork = {
  setResponses(next: Record<string, unknown>) {
    responses = next;
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

function installImageSources() {
  const src = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src')!;
  Object.defineProperty(HTMLImageElement.prototype, 'src', {
    ...src,
    set(value: string) {
      src.set!.call(this, imageSource(String(value)));
    },
  });
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name: string, value: string) {
    const next = this instanceof HTMLImageElement && name.toLowerCase() === 'src' ? imageSource(String(value)) : value;
    setAttribute.call(this, name, next);
  };
}

export function installDesignNetwork() {
  window.fetch = async (input: RequestInfo | URL) => {
    inflight += 1;
    try {
      const url = urlOf(input);
      const key = [`${url.pathname}${url.search}`, url.pathname].find((candidate) => candidate in responses);
      if (key === undefined) touched.add(url.pathname);
      const body = key === undefined ? {} : responses[key];
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
  installImageSources();
}
