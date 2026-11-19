// What this document has open on the network right now: requests sent through
// `apiFetch` that have not answered yet, and WebSockets to this origin (the
// terminals). A direct hand-over waits until both are zero, so it never closes
// a connection something of this page is still using
// (docs/plans/remote-direct-ios.md section 7).
let inFlight = 0;
const sockets = new Set<WebSocket>();

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

/** Counts `socket` while it is connecting or open. */
export function trackPageSocket(socket: WebSocket): void {
  sockets.add(socket);
}

// WebSocket.CLOSED, written out so a stand-in socket needs no constants.
const SOCKET_CLOSED = 3;

export function pageConnectionsQuiet(): boolean {
  for (const socket of Array.from(sockets)) {
    if (socket.readyState === SOCKET_CLOSED) sockets.delete(socket);
  }
  return inFlight === 0 && sockets.size === 0;
}

/** Test hook: forgets everything counted. */
export function resetPageConnectionsForTest(): void {
  inFlight = 0;
  sockets.clear();
}
