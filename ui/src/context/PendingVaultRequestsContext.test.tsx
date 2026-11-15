/* @vitest-environment jsdom */

// The shell's one read of the Vault requests waiting on the reader, which the Inbox lists and its
// entry counts (ui/design: inbox/approvals/desktop). It reads once and then only on what can
// change the list: no polling, no timer.
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { VaultRequest, WorkbenchEventHandlers } from './ApiContext';
import { usePendingVaultRequestsSource } from './PendingVaultRequestsContext';

const api = vi.hoisted(() => ({
  getVaultRequests: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
}));
const authorization = vi.hoisted(() => ({ capabilities: { can_use_vault_secrets: true } }));
vi.mock('./ApiContext', async (original) => ({ ...await original<typeof import('./ApiContext')>(), useApi: () => api }));
vi.mock('./InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => authorization }));

const request = (id: string, type: string) => ({ id, request_type: type, card: { request_type: type } }) as unknown as VaultRequest;
let handlers: WorkbenchEventHandlers;

beforeEach(() => {
  authorization.capabilities.can_use_vault_secrets = true;
  api.connectWorkbenchEvents.mockImplementation((next: WorkbenchEventHandlers) => {
    handlers = next;
    return () => {};
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('reads on mount and again on a Vault event, keeping only requests the reader answers', async () => {
  api.getVaultRequests.mockResolvedValueOnce({ ok: true, requests: [request('vrq_use', 'access'), request('vrq_other', 'reveal')] });
  const { result } = renderHook(() => usePendingVaultRequestsSource());
  await waitFor(() => expect(result.current.requests.map((r) => r.id)).toEqual(['vrq_use']));
  expect(api.getVaultRequests).toHaveBeenCalledWith({ status: 'pending' }, { handleError: false });

  api.getVaultRequests.mockResolvedValueOnce({ ok: true, requests: [] });
  act(() => handlers.onVaultsUpdated?.({ scope: 'request', request_id: 'vrq_use', request_status: 'approved' }));
  await waitFor(() => expect(result.current.requests).toEqual([]));
});

it('drops what it read under an old authorization at once', async () => {
  api.getVaultRequests.mockResolvedValueOnce({ ok: true, requests: [request('vrq_use', 'sign')] });
  const { result } = renderHook(() => usePendingVaultRequestsSource());
  await waitFor(() => expect(result.current.requests).toHaveLength(1));

  api.getVaultRequests.mockReturnValueOnce(new Promise(() => {}));
  act(() => handlers.onAuthorizationChanged?.({}));
  expect(result.current.requests).toEqual([]);
});

it('keeps the last list when a read fails', async () => {
  api.getVaultRequests.mockResolvedValueOnce({ ok: true, requests: [request('vrq_use', 'provision')] });
  const { result } = renderHook(() => usePendingVaultRequestsSource());
  await waitFor(() => expect(result.current.requests).toHaveLength(1));

  api.getVaultRequests.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  await act(async () => { result.current.refresh(); });
  expect(result.current.requests).toHaveLength(1);
});

it('reads nothing for a reader who may not use the Vault', () => {
  authorization.capabilities.can_use_vault_secrets = false;
  const { result } = renderHook(() => usePendingVaultRequestsSource());
  expect(result.current.requests).toEqual([]);
  expect(api.getVaultRequests).not.toHaveBeenCalled();
  expect(api.connectWorkbenchEvents).not.toHaveBeenCalled();
});
