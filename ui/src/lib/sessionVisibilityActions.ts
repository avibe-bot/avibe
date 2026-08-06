import type { ToastAction } from '../context/toastCoalesce';

type SessionVisibility = 'foreground' | 'background';
type SetSessionVisibility = (sessionId: string, visibility: SessionVisibility) => Promise<unknown>;
type ShowToast = (
  message: string,
  type?: 'success' | 'error' | 'warning',
  action?: ToastAction,
) => void;

export async function hideSessionToBackground(args: {
  sessionId: string;
  setSessionVisibility: SetSessionVisibility;
  showToast: ShowToast;
  hiddenMessage: string;
  undoLabel: string;
}): Promise<void> {
  const { sessionId, setSessionVisibility, showToast, hiddenMessage, undoLabel } = args;

  try {
    await setSessionVisibility(sessionId, 'background');
    showToast(hiddenMessage, 'success', {
      label: undoLabel,
      onClick: () => {
        void setSessionVisibility(sessionId, 'foreground');
      },
    });
  } catch (err) {
    showToast(err instanceof Error ? err.message : String(err), 'error');
  }
}
