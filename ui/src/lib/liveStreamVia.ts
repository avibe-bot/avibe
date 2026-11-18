// Whether this page's live event stream entered the instance through its direct ingress, as that
// stream's own handshake said (`via` in `/api/events`' `connected` frame). True only while that
// stream lives: it is cleared when the stream closes and answered again by the next one. Any
// other way in (the tunnel, the LAN, loopback) is not named; it is simply not direct.
let direct = false;
const listeners = new Set<() => void>();

export function setLiveStreamDirect(next: boolean): void {
  if (direct === next) return;
  direct = next;
  for (const listener of Array.from(listeners)) listener();
}

export function liveStreamDirect(): boolean {
  return direct;
}

export function subscribeLiveStreamDirect(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
