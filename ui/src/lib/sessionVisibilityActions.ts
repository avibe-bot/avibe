import type { ToastAction } from '../context/toastCoalesce';

export type SessionVisibility = 'foreground' | 'background';
type SetSessionVisibility = (sessionId: string, visibility: SessionVisibility) => Promise<unknown>;
type ShowToast = (
  message: string,
  type?: 'success' | 'error' | 'warning',
  action?: ToastAction,
) => void;

const opposite = (visibility: SessionVisibility): SessionVisibility =>
  visibility === 'background' ? 'foreground' : 'background';

/** Move a session to ``visibility`` and offer an Undo that moves it back.
 *  ``onCommitted`` reports every visibility the server accepted — the move and a
 *  later Undo alike — so a surface holding its own copy of the session row (the
 *  chat header) can keep its menu label in step with both. */
export async function changeSessionVisibility(args: {
  sessionId: string;
  visibility: SessionVisibility;
  setSessionVisibility: SetSessionVisibility;
  showToast: ShowToast;
  message: string;
  undoLabel: string;
  onCommitted?: (visibility: SessionVisibility) => void;
}): Promise<void> {
  const { sessionId, visibility, setSessionVisibility, showToast, message, undoLabel, onCommitted } = args;

  try {
    await setSessionVisibility(sessionId, visibility);
  } catch (err) {
    showToast(err instanceof Error ? err.message : String(err), 'error');
    return;
  }
  onCommitted?.(visibility);
  showToast(message, 'success', {
    label: undoLabel,
    onClick: () => {
      const previous = opposite(visibility);
      void setSessionVisibility(sessionId, previous).then(
        () => onCommitted?.(previous),
        // The request layer already toasted an HTTP refusal.
        () => undefined,
      );
    },
  });
}
