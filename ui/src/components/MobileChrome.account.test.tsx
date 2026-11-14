/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

const installBridge = (
  call: ReturnType<typeof vi.fn>,
  capabilities: Record<string, number> = { accountPresentation: 2 },
) => {
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities,
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
  it('draws the Account initial at 28 points in a 44-point target and opens the native account', async () => {
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
    expect(button.querySelector('span')?.className).toContain('size-7');
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

describe('MobileBrandHeader instance name (ui/design: mobile-app/switcher/b-home)', () => {
  const B = { accountPresentation: 2, app: 1, instancePresentation: 1 };
  const shell = (name: unknown) => vi.fn(async (method: string) => {
    if (method === 'app.info') {
      return { contract: 1, platform: 'ios', shellVersion: '1', capabilities: B, instance: { name } };
    }
    return null;
  });
  // The name is read once per document: each test loads the header as a new document would.
  let Header: typeof MobileBrandHeader;
  beforeEach(async () => {
    vi.resetModules();
    ({ MobileBrandHeader: Header } = await import('./MobileChrome'));
  });
  const header = (nativeAccountAction = true) => render(
    <MemoryRouter>
      <Header nativeAccountAction={nativeAccountAction} />
    </MemoryRouter>,
  );

  it('names the instance; the name opens the instances sheet and the avatar keeps the account', async () => {
    const call = shell('Studio');
    installBridge(call, B);
    header();
    const name = await screen.findByRole('button', { name: 'appShell.switchInstanceLabel' });
    expect(name.textContent).toBe('Studio');
    expect(screen.queryByText('appShell.title')).toBeNull();
    fireEvent.click(name);
    await waitFor(() => expect(call).toHaveBeenCalledWith('app.showInstances', {}));
    fireEvent.click(screen.getByRole('button', { name: 'appShell.accountMenuLabel' }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('app.showAccount', {}));
    // The name is read once per document, however often the header mounts.
    cleanup();
    header();
    expect(await screen.findByRole('button', { name: 'appShell.switchInstanceLabel' })).toBeTruthy();
    expect(call.mock.calls.filter(([method]) => method === 'app.info')).toHaveLength(1);
  });

  it('keeps "Avibe" without the capability', async () => {
    const older = shell('Studio');
    installBridge(older, { accountPresentation: 2, app: 1 });
    header();
    expect(screen.getByText('appShell.title')).toBeTruthy();
    expect(older).not.toHaveBeenCalledWith('app.info', {});
  });

  it('keeps "Avibe" when the shell gives no name', async () => {
    const blank = shell('  ');
    installBridge(blank, B);
    header();
    await waitFor(() => expect(blank).toHaveBeenCalledWith('app.info', {}));
    await Promise.resolve();
    expect(screen.getByText('appShell.title')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'appShell.switchInstanceLabel' })).toBeNull();
  });

  it('keeps "Avibe" where the header has no native account action', async () => {
    const call = shell('Studio');
    installBridge(call, B);
    header(false);
    await waitFor(() => expect(call).toHaveBeenCalledWith('app.info', {}));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText('appShell.title')).toBeTruthy();
  });
});
