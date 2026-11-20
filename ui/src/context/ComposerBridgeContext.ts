import { createContext, useContext, useEffect } from 'react';

/**
 * Bridges the chat composer (mounted only inside ChatPage) to components that
 * live outside it — e.g. the sidebar's "reference this session" action, which
 * needs to drop a `#<session>` mention into the currently-open chat's input.
 *
 * `target` is the active chat composer, or `null` when no chat is open. That
 * null/non-null state is exactly the "is the user on a Chat page?" gate the
 * sidebar menu item needs — no separate route check required.
 */
export type ComposerInsertTarget = {
  /** The session whose composer is currently mounted (the open chat). */
  sessionId: string;
  /** Insert a `#<session>` reference chip at the composer's cursor. */
  insertSessionReference: (sessionId: string, title?: string | null) => void;
};

/**
 * The open chat as an external composer source sees it (Ask Vibey from the
 * desktop shell). Published while a writable session has loaded, also when
 * its Show Page covers the composer, so the source can switch back to chat.
 */
export type ComposeTarget = {
  sessionId: string;
  /** The composer is mounted and can take text and files now. */
  composerReady: boolean;
  /** Show the conversation (not its Show Page), remembering that choice. */
  showChat: () => void;
  /** Append on a new line; false when the composer refuses (voice capture). */
  appendLine: (text: string) => boolean;
  /** Stage and upload files through the composer's own attachment path. */
  addFiles: (files: File[]) => void;
};

type ComposerBridge = {
  target: ComposerInsertTarget | null;
  setTarget: (target: ComposerInsertTarget | null) => void;
  compose?: ComposeTarget | null;
  setCompose?: (target: ComposeTarget | null) => void;
};

export const ComposerBridgeContext = createContext<ComposerBridge | null>(null);

/** Consumer side (sidebar session menu): the active composer target, or null. */
export const useComposerInsertTarget = (): ComposerInsertTarget | null =>
  useContext(ComposerBridgeContext)?.target ?? null;

/**
 * Producer side (ChatPage): publish `target` while this chat is mounted, clear
 * it on unmount / session change. Only one ChatPage is mounted at a time, so a
 * plain set-on-mount / clear-on-unmount is safe. `target` MUST be memoized by
 * the caller (e.g. on sessionId) so the effect doesn't re-run every render.
 */
export const useRegisterComposerTarget = (
  target: ComposerInsertTarget | null,
  compose: ComposeTarget | null = null,
): void => {
  const bridge = useContext(ComposerBridgeContext);
  const setTarget = bridge?.setTarget;
  const setCompose = bridge?.setCompose;
  useEffect(() => {
    if (!setTarget) return;
    setTarget(target);
    return () => setTarget(null);
  }, [setTarget, target]);
  // `compose` follows the same rules; it MUST be memoized by the caller too.
  useEffect(() => {
    if (!setCompose) return;
    setCompose(compose);
    return () => setCompose(null);
  }, [setCompose, compose]);
};

/** Consumer side (desktop compose receiver): the open chat, or null. */
export const useComposeTarget = (): ComposeTarget | null =>
  useContext(ComposerBridgeContext)?.compose ?? null;

