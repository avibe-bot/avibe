// Design frames never reach a server. Every request is answered here: from the
// frame's `api` fixtures, else with an empty object. Paths a frame touched without
// a fixture are recorded, so the screenshot tool can report what a frame depends on.

const touched = new Set<string>();
let responses: Record<string, unknown> = {};

export const designNetwork = {
  setResponses(next: Record<string, unknown> | undefined) {
    responses = next ?? {};
  },
  unfixtured(): string[] {
    return [...touched].sort();
  },
};

function pathOf(input: RequestInfo | URL): string {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(raw, window.location.href).pathname;
}

export function installDesignNetwork() {
  window.fetch = async (input: RequestInfo | URL) => {
    const path = pathOf(input);
    if (!(path in responses)) touched.add(path);
    const body = path in responses ? responses[path] : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
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
      touched.add(`EventSource ${pathOf(url)}`);
    }
    close() {}
  }
  window.EventSource = SilentEventSource as unknown as typeof EventSource;
}
