// Design frames never reach a server. Every request is answered here: from the
// frame's API fixtures (keyed by path plus query, else by path), else with an empty
// object. Paths a frame touched without a fixture are recorded, so the screenshot
// tool can report what a frame depends on.

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
}
