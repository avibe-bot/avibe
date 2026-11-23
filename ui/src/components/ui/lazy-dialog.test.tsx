/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastContext } from '../../context/ToastContext';
import { lazyPage } from '../../lib/lazyPage';
import { LazyDialog } from './lazy-dialog';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

afterEach(cleanup);

describe('LazyDialog', () => {
  it('loses only the dialog when its code cannot load: a toast, the owner closes, nothing drawn in place', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const Missing = lazyPage<object>(() => Promise.reject(new TypeError('Failed to fetch dynamically imported module')));
    const showToast = vi.fn();
    const onFailed = vi.fn();
    await act(async () => {
      render(
        <ToastContext.Provider value={{ showToast }}>
          <main>the page behind</main>
          <LazyDialog onFailed={onFailed}><Missing /></LazyDialog>
        </ToastContext.Provider>,
      );
    });
    expect(onFailed).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith('errorBoundary.chunkLoadBody', 'error');
    expect(screen.getByRole('main').textContent).toBe('the page behind');
    expect(screen.queryByText('errorBoundary.chunkLoadTitle')).toBeNull();
  });

  it("says an error of the loaded dialog's own is not an update to reload for", async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const Broken = () => { throw new Error('render bug'); };
    const showToast = vi.fn();
    const onFailed = vi.fn();
    await act(async () => {
      render(
        <ToastContext.Provider value={{ showToast }}>
          <LazyDialog onFailed={onFailed}><Broken /></LazyDialog>
        </ToastContext.Provider>,
      );
    });
    expect(onFailed).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith('errorBoundary.body', 'error');
  });
});
