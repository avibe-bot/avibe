import { describe, expect, it, vi } from 'vitest';

import { hideSessionToBackground } from './sessionVisibilityActions';

describe('hideSessionToBackground', () => {
  it('moves the session to the background and exposes an undo action', async () => {
    const setSessionVisibility = vi.fn().mockResolvedValue(undefined);
    const showToast = vi.fn();

    await hideSessionToBackground({
      sessionId: 'sess-1',
      setSessionVisibility,
      showToast,
      hiddenMessage: 'Session hidden',
      undoLabel: 'Undo',
    });

    expect(setSessionVisibility).toHaveBeenCalledWith('sess-1', 'background');
    expect(showToast).toHaveBeenCalledWith(
      'Session hidden',
      'success',
      expect.objectContaining({ label: 'Undo' }),
    );

    const action = showToast.mock.calls[0][2];
    action.onClick();
    expect(setSessionVisibility).toHaveBeenLastCalledWith('sess-1', 'foreground');
  });

  it('surfaces visibility failures without offering undo', async () => {
    const setSessionVisibility = vi.fn().mockRejectedValue(new Error('Visibility update failed'));
    const showToast = vi.fn();

    await hideSessionToBackground({
      sessionId: 'sess-1',
      setSessionVisibility,
      showToast,
      hiddenMessage: 'Session hidden',
      undoLabel: 'Undo',
    });

    expect(showToast).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith('Visibility update failed', 'error');
  });
});
