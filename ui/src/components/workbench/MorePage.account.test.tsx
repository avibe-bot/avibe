/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MoreAccountSection } from './MorePage';

const auth = vi.hoisted(() => ({
  email: 'alex@example.com' as string | null,
  signingOut: false,
  signOut: vi.fn(),
}));

vi.mock('../../lib/useAuthAccount', () => ({
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

describe('MoreAccountSection', () => {
  it('keeps web sign-out when account presentation is unavailable', async () => {
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

    render(<MoreAccountSection />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.signOut' }));

    await waitFor(() => expect(auth.signOut).toHaveBeenCalledTimes(1));
  });

  it('opens the account sheet on its Account page where the shell presents instances, as before elsewhere', async () => {
    const call = vi.fn(async () => null);
    const shell = (capabilities: Record<string, number>) => Object.defineProperty(window, 'avibeNative', {
      configurable: true,
      value: { contract: 1, platform: 'ios', shellVersion: '1.0 (1)', capabilities, call, on: vi.fn(() => () => undefined) },
    });

    shell({ accountPresentation: 2, app: 1, instancePresentation: 1 });
    render(<MoreAccountSection />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.openMobileAccount' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('app.showAccount', { start: 'account' }));
    cleanup();

    shell({ accountPresentation: 2, app: 1 });
    render(<MoreAccountSection />);
    fireEvent.click(screen.getByRole('button', { name: 'appShell.openMobileAccount' }));
    await waitFor(() => expect(call).toHaveBeenLastCalledWith('app.showAccount', {}));
    expect(call).toHaveBeenCalledTimes(2);
    expect(auth.signOut).not.toHaveBeenCalled();
  });

  it('falls back to web sign-out when native presentation rejects', async () => {
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

    render(<MoreAccountSection />);
    fireEvent.click(
      screen.getByRole('button', { name: 'appShell.openMobileAccount' }),
    );

    await waitFor(() => expect(auth.signOut).toHaveBeenCalledTimes(1));
  });
});
