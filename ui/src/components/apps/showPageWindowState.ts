import { showPageEmbeddedPath, showPagePrivatePath } from '../../apps/showPageAvatar';

export type ShowPageWindowStatus = 'loading' | 'ready' | 'missing';

/** Apply a silent session read without withdrawing a working frame on transient errors. */
export function showPageWindowStatusAfterRead(
  current: ShowPageWindowStatus,
  result: { status: number | null; session: unknown },
): ShowPageWindowStatus {
  if (result.status === 404) return 'missing';
  const session = result.session;
  if (!session || typeof session !== 'object' || typeof (session as { id?: unknown }).id !== 'string') {
    return current === 'ready' ? 'ready' : 'missing';
  }
  return (session as { status?: unknown }).status === 'archived' ? 'missing' : 'ready';
}

/** The framed page and its parent-owned controls share one lifecycle source. */
export function showPageWindowSource(
  sessionId: string,
  status: ShowPageWindowStatus,
  archived: boolean,
): string | null {
  if (!sessionId || status === 'missing' || archived) return null;
  return showPageEmbeddedPath(showPagePrivatePath(sessionId));
}

/**
 * A native Show window's title, which the shell takes from the document title: the Show's title as
 * last read or renamed, else the launcher's. `undefined` means the Show is not known yet; `null` or
 * blank means it is and has no title, which reads as `untitled`. `null` here leaves the title as is.
 */
export function showPageWindowTitle(known: string | null | undefined, launched: unknown, untitled: string): string | null {
  if (known !== undefined) return known?.trim() || untitled;
  return typeof launched === 'string' && launched.trim() ? launched.trim() : null;
}
