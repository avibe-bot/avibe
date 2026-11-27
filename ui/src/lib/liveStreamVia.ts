// How this page reaches the instance, as the instance itself said, per connection. `direct` through
// the direct ingress, `other` for any other way in (the tunnel, the LAN, loopback), which is never
// named. `null` is unknown, never "not direct".
//
// Two connections speak, because the live event stream is a connection of its own:
// - the stream, in its own handshake (`via` in `/api/events`' `connected` frame). Cleared when the
//   stream closes and answered again by the next one;
// - the page's requests, on each answer (`X-Avibe-Via`, read by the page's own `fetch`). The word of
//   the latest request that has been answered is kept until a later one says otherwise.
//
// What the page shows and follows is both together (`pageVia`): `direct` only when the stream and
// the latest request both say so, `other` as soon as either does.
export type LiveStreamVia = 'direct' | 'other' | null;

let stream: LiveStreamVia = null;
let request: LiveStreamVia = null;
let started = 0;
let answered = 0;
const listeners = new Set<() => void>();

function changed(): void {
  for (const listener of Array.from(listeners)) listener();
}

export function setLiveStreamVia(next: LiveStreamVia): void {
  if (stream === next) return;
  stream = next;
  changed();
}

/** The stream's own word. */
export function liveStreamVia(): LiveStreamVia {
  return stream;
}

/** Numbers a request as it is sent, so that answers are taken in the order the requests began. */
export function beginRequestVia(): number {
  started += 1;
  return started;
}

/**
 * The answer to request `order` said how it arrived. One that says nothing (an answer the instance
 * did not make for a remote session) changes nothing, and neither does the answer to a request
 * that began before one already heard: a slow answer on an old connection is not the latest word.
 */
export function noteRequestVia(order: number, word: string | null): void {
  if ((word !== 'direct' && word !== 'other') || order < answered) return;
  answered = order;
  if (request === word) return;
  request = word;
  changed();
}

/** The latest request's own word. */
export function latestRequestVia(): LiveStreamVia {
  return request;
}

/** The stream and the latest request together. */
export function pageVia(): LiveStreamVia {
  if (stream === 'other' || request === 'other') return 'other';
  return stream === 'direct' && request === 'direct' ? 'direct' : null;
}

/** Called when the stream's word or the latest request's changes. */
export function subscribePageVia(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
