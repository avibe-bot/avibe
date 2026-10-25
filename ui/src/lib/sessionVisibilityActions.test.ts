import { describe, expect, it, vi } from 'vitest';

import { changeSessionVisibility } from './sessionVisibilityActions';

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('changeSessionVisibility', () => {
  it.each([
    ['background', 'foreground'],
    ['foreground', 'background'],
  ] as const)('moves the session to the %s, reports it, and undoes back to the %s', async (visibility, previous) => {
    const setSessionVisibility = vi.fn().mockResolvedValue(undefined);
    const showToast = vi.fn();
    const onCommitted = vi.fn();

    await changeSessionVisibility({
      sessionId: 'sess-1',
      visibility,
      setSessionVisibility,
      showToast,
      message: 'Moved',
      undoLabel: 'Undo',
      onCommitted,
    });

    expect(setSessionVisibility).toHaveBeenCalledWith('sess-1', visibility);
    expect(onCommitted).toHaveBeenLastCalledWith(visibility);
    expect(showToast).toHaveBeenCalledWith('Moved', 'success', expect.objectContaining({ label: 'Undo' }));

    showToast.mock.calls[0][2].onClick();
    await flush();
    expect(setSessionVisibility).toHaveBeenLastCalledWith('sess-1', previous);
    expect(onCommitted).toHaveBeenLastCalledWith(previous);
  });

  it('surfaces visibility failures without reporting a change or offering undo', async () => {
    const setSessionVisibility = vi.fn().mockRejectedValue(new Error('Visibility update failed'));
    const showToast = vi.fn();
    const onCommitted = vi.fn();

    await changeSessionVisibility({
      sessionId: 'sess-1',
      visibility: 'background',
      setSessionVisibility,
      showToast,
      message: 'Moved',
      undoLabel: 'Undo',
      onCommitted,
    });

    expect(showToast).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith('Visibility update failed', 'error');
    expect(onCommitted).not.toHaveBeenCalled();
  });
});
