import { showPageEmbeddedPath, showPagePrivatePath } from '../../apps/showPageAvatar';

export type ShowPageWindowStatus = 'loading' | 'ready' | 'missing';

/** The framed page and its parent-owned controls share one lifecycle source. */
export function showPageWindowSource(
  sessionId: string,
  status: ShowPageWindowStatus,
  archived: boolean,
): string | null {
  if (!sessionId || status === 'missing' || archived) return null;
  return showPageEmbeddedPath(showPagePrivatePath(sessionId));
}
