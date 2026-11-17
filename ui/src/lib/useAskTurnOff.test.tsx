// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const sandbox = vi.hoisted(() => ({ open: vi.fn(() => null) }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => ({}) }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  getVaultSandboxClient: async () => ({ supports: async () => true }),
  openVaultAuthorizationWindow: sandbox.open,
}));

import { useAskTurnOff } from './useAskTurnOff';
import { recordVaultPasskeyOutcome } from './vaultBrowserHandoff';
import { VaultSandboxError } from './vaultSandboxClient';

afterEach(() => {
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__');
});

// Turning off ask-before-signing is a passkey step. A desktop app without the capability never
// offers it; one whose in-app step just failed must stop offering it too, rather than leave a switch
// whose next click could only fail.
describe('ask-before-signing switch in a desktop app that runs passkey steps', () => {
  it('stops offering the switch once a passkey step failed in the app', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { configurable: true, value: true });
    const { result } = renderHook(() => useAskTurnOff({ name: 'TREASURY_KEY' }, true, vi.fn()));
    await waitFor(() => expect(result.current.state.phase).toBe('ready'));

    act(() => recordVaultPasskeyOutcome(new VaultSandboxError('sign_failed', 'sandbox failure')));
    expect(result.current.state.phase).toBe('unavailable');
    act(() => result.current.turnOff());
    expect(sandbox.open).not.toHaveBeenCalled();
  });
});
