// What this document has open on the network right now, for the direct
// hand-over (docs/plans/remote-direct-ios.md section 7): every same-origin
// `fetch` until its body has been read, cancelled, or has not moved for a
// while; WebSockets to this origin (the terminals); and same-origin iframes
// (a Show Page runs its own requests). The hand-over waits until there are
// none, so it never closes a connection something of this page still uses.
// Subresources the browser loads by itself (images, scripts, styles) are not
// seen.
import { beginRequestVia, noteRequestVia } from './liveStreamVia';

let inFlight = 0;
const sockets = new Set<WebSocket>();
let installed = false;
/** While a hand-over closes quiet connections, new requests wait here. */
let hold: Promise<void> | null = null;

/** A body that has not moved for this long no longer keeps the page busy. */
const BODY_IDLE_RELEASE_MS = 10_000;
// WebSocket.CLOSED, written out so a stand-in socket needs no constants.
const SOCKET_CLOSED = 3;

/** Counts one request until `done` is called (once). */
export function beginPageRequest(): () => void {
  inFlight += 1;
  let ended = false;
  return () => {
    if (ended) return;
    ended = true;
    inFlight -= 1;
  };
}

/**
 * Counts `socket` while it is connecting or open. Only where a hand-over can
 * happen (`installPageFetchCounting` ran); elsewhere nothing is kept.
 */
export function trackPageSocket(socket: WebSocket): void {
  if (!installed) return;
  sockets.add(socket);
  if (typeof socket.addEventListener === 'function') {
    socket.addEventListener('close', () => sockets.delete(socket), { once: true });
  }
}

function sameOrigin(input: RequestInfo | URL): boolean {
  try {
    const raw = typeof input === 'string' || input instanceof URL ? input : input.url;
    return new URL(raw, window.location.href).origin === window.location.origin;
  } catch {
    return false;
  }
}

/**
 * The same response, whose body calls `done` once it has been read to the end,
 * or has not moved for BODY_IDLE_RELEASE_MS (a body nobody reads, a cancel, a
 * failure). A response without a body is done at once.
 */
function countedUntilBodyEnds(response: Response, done: () => void): Response {
  const body = response.body;
  if (
    !body
    || typeof TransformStream === 'undefined'
    || response.status < 200
    || response.status > 599
  ) {
    done();
    return response;
  }
  let idle: ReturnType<typeof setTimeout> | null = null;
  const release = () => {
    if (idle !== null) clearTimeout(idle);
    idle = null;
    done();
  };
  const rearm = () => {
    if (idle !== null) clearTimeout(idle);
    idle = setTimeout(release, BODY_IDLE_RELEASE_MS);
  };
  rearm();
  const counted = body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      rearm();
      controller.enqueue(chunk);
    },
    flush() {
      release();
    },
  }));
  const wrapped = new Response(counted, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
  for (const name of ['url', 'redirected', 'type'] as const) {
    Object.defineProperty(wrapped, name, { value: response[name] });
  }
  return wrapped;
}

/**
 * Counts this document's same-origin `fetch` requests from now on. Installed
 * once, only where a hand-over can happen (the App with `direct` version 2).
 */
export function installPageFetchCounting(): void {
  if (installed || typeof window === 'undefined' || typeof window.fetch !== 'function') return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!sameOrigin(input)) return original(input, init);
    // Sent only after a hand-over in progress has finished, so it cannot
    // reuse a connection the hand-over is about to close.
    while (hold) await hold;
    const done = beginPageRequest();
    const order = beginRequestVia();
    let response: Response;
    try {
      response = await original(input, init);
    } catch (error) {
      done();
      throw error;
    }
    // How this request arrived, where the instance said so on its answer.
    noteRequestVia(order, response.headers.get('X-Avibe-Via'));
    return countedUntilBodyEnds(response, done);
  };
}

/**
 * Holds this document's new same-origin requests until the returned function
 * is called. New WebSockets are not held: each opens a connection of its own,
 * which takes the path new connections prefer.
 */
export function holdPageRequests(): () => void {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  hold = held;
  return () => {
    if (hold === held) hold = null;
    release();
  };
}

function sameOriginFrameMounted(): boolean {
  if (typeof document === 'undefined') return false;
  for (const frame of Array.from(document.querySelectorAll('iframe'))) {
    const src = frame.getAttribute('src');
    if (!src) continue;
    try {
      const url = new URL(src, window.location.href);
      if (
        (url.protocol === 'https:' || url.protocol === 'http:')
        && url.origin === window.location.origin
      ) return true;
    } catch {
      // Not a URL this page could load.
    }
  }
  return false;
}

export function pageConnectionsQuiet(): boolean {
  for (const socket of Array.from(sockets)) {
    if (socket.readyState === SOCKET_CLOSED) sockets.delete(socket);
  }
  return inFlight === 0 && sockets.size === 0 && !sameOriginFrameMounted();
}
