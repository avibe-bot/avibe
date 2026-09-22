/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { ToastProvider } from './ToastProvider';
import { useToast } from './ToastContext';

afterEach(cleanup);

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
});
