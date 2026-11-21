import { useEffect } from 'react';
import { isNativeAppWindow } from '../lib/desktopShell';

/** The confirmation a native App window shows before its close discards page state. */
export type NativeWindowCloseConfirmation = {
  message: string;
  confirm: string;
  cancel: string;
};

declare global {
  interface Window {
    /**
     * Asked by the desktop shell when the user closes this native App window or
     * quits. `null` closes at once; a confirmation is shown as a native dialog,
     * and only its confirm button closes the window. Page-owned and advisory:
     * a missing, throwing or slow answer closes the window as before.
     */
    __AVIBE_APP_WINDOW_CLOSE_GUARD__?: () => NativeWindowCloseConfirmation | null;
  }
}

const confirmations = new Set<() => NativeWindowCloseConfirmation | null>();

function pendingConfirmation(): NativeWindowCloseConfirmation | null {
  for (const read of confirmations) {
    const confirmation = read();
    if (confirmation) return confirmation;
  }
  return null;
}

/**
 * Adds a source of unsaved state to this native App window's close and quit
 * question, and returns its removal. A browser tab or the Workbench window has
 * no native close to guard, so this is inert there; those surfaces keep their
 * own unload and route guards.
 */
export function registerNativeWindowCloseConfirmation(
  read: () => NativeWindowCloseConfirmation | null,
): () => void {
  if (!isNativeAppWindow()) return () => undefined;
  confirmations.add(read);
  window.__AVIBE_APP_WINDOW_CLOSE_GUARD__ = pendingConfirmation;
  return () => {
    confirmations.delete(read);
  };
}

/** A page body's own unsaved state, for a body that is the native window's route. */
export function useNativeWindowCloseGuard(confirmation: NativeWindowCloseConfirmation | null): void {
  const message = confirmation?.message ?? null;
  const confirm = confirmation?.confirm ?? '';
  const cancel = confirmation?.cancel ?? '';
  useEffect(() => {
    if (!message) return;
    return registerNativeWindowCloseConfirmation(() => ({ message, confirm, cancel }));
  }, [message, confirm, cancel]);
}
