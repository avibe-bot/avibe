// The reader's access digest: what ``/api/session`` says the server lets this
// reader read, as a digest (``access_digest``). An ``authorization.changed``
// event clears what this device stores for the reader, but only a tab whose
// event stream is up receives it. The digest last seen is kept here, shared by
// every tab and outliving them all, so the next session check (an open, or a
// stream that reconnects) notices a change no tab heard of
// (docs/plans/local-sync-store.md, access digest).

const ACCESS_DIGEST_KEY = 'avibe.access-digest.v1';

type Recorded = { reader: string; digest: string };

function readRecorded(): Recorded | null {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(ACCESS_DIGEST_KEY) ?? 'null');
    if (typeof value !== 'object' || value === null) return null;
    const { reader, digest } = value as Partial<Recorded>;
    return typeof reader === 'string' && typeof digest === 'string' ? { reader, digest } : null;
  } catch {
    return null;
  }
}

/** Whether ``digest`` differs from the one last recorded for ``reader``. A first
 *  digest is no change, and neither is one recorded for another reader: a
 *  different reader empties the stores on its own. */
export function accessDigestChanged(reader: string, digest: string): boolean {
  const recorded = readRecorded();
  return recorded !== null && recorded.reader === reader && recorded.digest !== digest;
}

/** Record ``digest`` as ``reader``'s, once whatever a change voids is gone. */
export function recordAccessDigest(reader: string, digest: string): void {
  try {
    window.localStorage.setItem(ACCESS_DIGEST_KEY, JSON.stringify({ reader, digest } satisfies Recorded));
  } catch {
    // Without storage every check is a first one; nothing is compared.
  }
}
