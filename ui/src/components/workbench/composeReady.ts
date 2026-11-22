/**
 * Whether Ask Vibey (desktop shell) may put content into the open chat's
 * composer now. The composer must be on screen (not covered by the chat's Show
 * Page) and this route's bootstrap must have reconciled the saved draft: an
 * append before that marks the box edited, and Composer would then skip the
 * saved draft that arrives with the bootstrap (written on another device, say).
 *
 * The rest of "can take content" is answered at the moment of insertion by the
 * composer itself: `appendLine` / `stageFiles` return false while it is not
 * mounted, voice capture owns the draft, or (for files) a send is in flight or
 * it is disabled; a read-only chat publishes no target at all.
 */
export function composeReady({
  sessionId,
  showPageActive,
  hydratedSessionId,
}: {
  sessionId: string;
  showPageActive: boolean;
  hydratedSessionId: string | null;
}): boolean {
  return !showPageActive && hydratedSessionId === sessionId;
}
