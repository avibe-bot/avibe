/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MobileBrandHeader } from './MobileChrome';

const auth = vi.hoisted(() => ({
  email: 'max@example.com' as string | null,
  signingOut: false,
  signOut: vi.fn(async () => undefined),
}));

vi.mock('../lib/useAuthAccount', () => ({
  useAuthAccount: () => auth,
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));
vi.mock('./InstallHint', () => ({ InstallHint: () => null }));

const installBridge = (call: ReturnType<typeof vi.fn>) => {
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: { accountPresentation: 2 },
      call,
      on: vi.fn(() => () => undefined),
    },
  });
};

afterEach(() => {
  cleanup();
  auth.email = 'max@example.com';
  auth.signingOut = false;
  auth.signOut.mockClear();
  Reflect.deleteProperty(window, 'avibeNative');
});

describe('MobileBrandHeader native account action', () => {
  it('uses the Account initial in a 44-point target and opens the native account', async () => {
    const call = vi.fn(async () => null);
    installBridge(call);
    render(
      <MemoryRouter>
        <MobileBrandHeader nativeAccountAction />
      </MemoryRouter>,
    );

    const button = screen.getByRole('button', { name: 'appShell.accountMenuLabel' });
    expect(button.className).toContain('size-11');
    expect(button.textContent).toBe('M');
    expect(button.querySelector('span')?.className).toContain('size-9');
    fireEvent.click(button);

    await waitFor(() => {
      expect(call).toHaveBeenCalledWith('app.showAccount', {});
    });
    expect(auth.signOut).not.toHaveBeenCalled();
  });

  it('uses the existing Web sign-out path when native presentation rejects', async () => {
    installBridge(vi.fn(async () => {
      throw new Error('unsupported');
    }));
    render(
      <MemoryRouter>
        <MobileBrandHeader nativeAccountAction />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));
    await waitFor(() => expect(auth.signOut).toHaveBeenCalledTimes(1));
  });

  it('keeps ordinary browser headers unchanged', () => {
    render(
      <MemoryRouter>
        <MobileBrandHeader />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('button', { name: 'appShell.accountMenuLabel' })).toBeNull();
  });
});
