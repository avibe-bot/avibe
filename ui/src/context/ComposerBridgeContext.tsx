import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

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

type ComposerBridge = {
  target: ComposerInsertTarget | null;
  setTarget: (target: ComposerInsertTarget | null) => void;
};

const ComposerBridgeContext = createContext<ComposerBridge | null>(null);

export const ComposerBridgeProvider = ({ children }: { children: ReactNode }) => {
  const [target, setTarget] = useState<ComposerInsertTarget | null>(null);
  return (
    <ComposerBridgeContext.Provider value={{ target, setTarget }}>{children}</ComposerBridgeContext.Provider>
  );
};

/** Consumer side (sidebar session menu): the active composer target, or null. */
export const useComposerInsertTarget = (): ComposerInsertTarget | null =>
  useContext(ComposerBridgeContext)?.target ?? null;

/**
 * Producer side (ChatPage): publish `target` while this chat is mounted, clear
 * it on unmount / session change. Only one ChatPage is mounted at a time, so a
 * plain set-on-mount / clear-on-unmount is safe. `target` MUST be memoized by
 * the caller (e.g. on sessionId) so the effect doesn't re-run every render.
 */
export const useRegisterComposerTarget = (target: ComposerInsertTarget | null): void => {
  const setTarget = useContext(ComposerBridgeContext)?.setTarget;
  useEffect(() => {
    if (!setTarget) return;
    setTarget(target);
    return () => setTarget(null);
  }, [setTarget, target]);
};
