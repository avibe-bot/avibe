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
