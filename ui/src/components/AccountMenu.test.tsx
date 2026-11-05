/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AccountMenu } from './AccountMenu';

const auth = vi.hoisted(() => ({
  email: 'alex@example.com' as string | null,
  signingOut: false,
  signOut: vi.fn(),
}));

vi.mock('../lib/useAuthAccount', () => ({
  useAuthAccount: () => auth,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

beforeEach(() => {
  auth.email = 'alex@example.com';
  auth.signingOut = false;
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, 'avibeNative');
  vi.clearAllMocks();
});

describe('AccountMenu', () => {
  it('closes its open menu and marks Escape as consumed', () => {
    render(<AccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));

    const menuItem = screen.getByRole('menuitem');
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    act(() => menuItem.dispatchEvent(event));

    expect(event.defaultPrevented).toBe(true);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('opens the app account surface instead of globally signing out in a native shell', async () => {
    const call = vi.fn(async () => null);
    Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: {
        contract: 1,
        platform: 'ios',
        shellVersion: '1.0 (1)',
        capabilities: { accountPresentation: 1, app: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
    });

    render(<AccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'appShell.openMobileAccount' }));

    await waitFor(() => expect(call).toHaveBeenCalledWith('app.showAccount', {}));
    expect(auth.signOut).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('keeps the web sign-out action for an older shell', async () => {
    Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: {
        contract: 1,
        platform: 'ios',
        shellVersion: '0.9 (9)',
        capabilities: { app: 1 },
        call: vi.fn(),
        on: vi.fn(() => () => undefined),
      },
    });

    render(<AccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'appShell.signOut' }));

    await waitFor(() => expect(auth.signOut).toHaveBeenCalledTimes(1));
  });

  it('falls back to web sign-out when native account presentation fails', async () => {
    Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: {
        contract: 1,
        platform: 'ios',
        shellVersion: '1.0 (1)',
        capabilities: { accountPresentation: 1, app: 1 },
        call: vi.fn(async () => {
          throw { code: 'failed', message: 'unavailable' };
        }),
        on: vi.fn(() => () => undefined),
      },
    });

    render(<AccountMenu />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'appShell.openMobileAccount' }));

    await waitFor(() => expect(auth.signOut).toHaveBeenCalledTimes(1));
  });
});
