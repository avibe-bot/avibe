/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from './ToastProvider';
import { useToast } from './ToastContext';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const Trigger = () => {
  const { showToast } = useToast();
  return <button type="button" onClick={() => showToast('Saved')}>Save</button>;
};

describe('ToastProvider', () => {
  it('announces toast messages through a polite status region', async () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    const toast = screen.getByRole('status');
    expect(toast.textContent).toContain('Saved');
    expect(toast.getAttribute('aria-live')).toBe('polite');
    expect(toast.getAttribute('aria-atomic')).toBe('true');
  });

  it('refreshes the dismissal lifetime when a visible toast is coalesced', () => {
    vi.useFakeTimers();
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    act(() => vi.advanceTimersByTime(2500));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    act(() => vi.advanceTimersByTime(2999));
    expect(screen.getByRole('status')).toBeTruthy();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole('status')).toBeNull();
  });
});
