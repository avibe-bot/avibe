// @vitest-environment jsdom
// A protected seed-phrase wallet whose window or passkey failed in a desktop app that runs these
// steps moves the page to the browser, and the form's hand-off says so. The wallet panel must not
// also tell the user that the browser blocked a pop-up, advice that is wrong inside the app.
import { act, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const sandbox = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock('@/context/ApiContext', () => ({ useApi: () => ({}) }));
vi.mock('./vaultSandboxClient', async (original) => ({
  ...await original<typeof import('./vaultSandboxClient')>(),
  openVaultAuthorizationWindow: sandbox.open,
}));

// The hand-off is page-lifetime state: each case is a new page.
async function freshPage() {
  vi.resetModules();
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { configurable: true, value: true });
  const { useProtectedHdCreate } = await import('./useProtectedHdCreate');
  const handoff = await import('./vaultBrowserHandoff');
  const { VaultSandboxError } = await import('./vaultSandboxClient');
  return { useProtectedHdCreate, record: handoff.recordVaultPasskeyOutcome, VaultSandboxError };
}

const start = (seal: () => Promise<never>) => ({
  name: 'COLD_WALLET', mode: 'create' as const, metadata: {}, seal, onSaved: vi.fn(),
});

afterEach(() => {
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__');
  sandbox.open.mockReset();
});

it('says nothing of its own when the app refused the window', async () => {
  const page = await freshPage();
  // The real client records the refused window before returning null.
  sandbox.open.mockImplementation(() => {
    page.record(new page.VaultSandboxError('authorization_popup_blocked', 'refused'));
    return null;
  });
  const { result } = renderHook(() => page.useProtectedHdCreate());
  await act(() => result.current.start(start(() => Promise.reject(new Error('not reached')))));
  expect(result.current.phase).toEqual({ kind: 'idle' });
});

it('says nothing of its own when its failure moved the app to the browser', async () => {
  const page = await freshPage();
  sandbox.open.mockReturnValue({ id: 'win', close: vi.fn() });
  const error = new page.VaultSandboxError('seal_hd_failed', 'sandbox failure');
  const { result } = renderHook(() => page.useProtectedHdCreate());
  // Plays the sandbox client, which records how the step ended before its caller sees the error.
  await act(() => result.current.start(start(async () => { page.record(error); throw error; })));
  expect(result.current.phase).toEqual({ kind: 'idle' });
});

it('still reports a failure that did not move the app', async () => {
  const page = await freshPage();
  sandbox.open.mockReturnValue({ id: 'win', close: vi.fn() });
  const { result } = renderHook(() => page.useProtectedHdCreate());
  await act(() => result.current.start(start(async () => { throw new page.VaultSandboxError('context_expired', 'expired'); })));
  expect(result.current.phase.kind).toBe('failed');
});
