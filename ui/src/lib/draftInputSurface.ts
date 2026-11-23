/** A per-tab draft entrance. It survives route remounts, never travels as device identity. */
export type DraftInputSurface = 'ask_vibey' | undefined;
const prefix = 'avibe.draft-input-surface.';

export function readDraftInputSurface(sessionId?: string | null): DraftInputSurface {
  if (!sessionId) return undefined;
  try { return sessionStorage.getItem(prefix + sessionId) === 'ask_vibey' ? 'ask_vibey' : undefined; }
  catch { return undefined; } // Storage-denied browsers retain the mounted composer's ref.
}

export function writeDraftInputSurface(sessionId: string | null | undefined, surface: DraftInputSurface): void {
  if (!sessionId) return;
  try {
    if (surface) sessionStorage.setItem(prefix + sessionId, surface);
    else sessionStorage.removeItem(prefix + sessionId);
  } catch { /* Presentation metadata must not prevent sending. */ }
}
