// How this page's live event stream entered the instance, as that stream's own handshake said
// (`via` in `/api/events`' `connected` frame): `direct` through the direct ingress, `other` for any
// other way in (the tunnel, the LAN, loopback), which is never named. `null` while no live stream
// has answered (before the handshake, while reconnecting, or a server too old to say): unknown,
// never "not direct". Cleared when the stream closes and answered again by the next one.
export type LiveStreamVia = 'direct' | 'other' | null;

let via: LiveStreamVia = null;
const listeners = new Set<() => void>();

export function setLiveStreamVia(next: LiveStreamVia): void {
  if (via === next) return;
  via = next;
  for (const listener of Array.from(listeners)) listener();
}

export function liveStreamVia(): LiveStreamVia {
  return via;
}

export function subscribeLiveStreamVia(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
