/* @vitest-environment jsdom */

import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { usePendingVaultRequests } from './usePendingVaultRequests';

type Handlers = {
  onAuthorizationChanged?: (data: unknown) => void;
  onEventBridgeStatus?: (data: { connected: boolean }) => void;
};

const handlers: Handlers[] = [];
const connectWorkbenchEvents = vi.fn((next: Handlers) => {
  handlers.push(next);
  return () => {
    const at = handlers.indexOf(next);
    if (at >= 0) handlers.splice(at, 1);
  };
});
const getVaultRequests = vi.fn();

vi.mock('@/context/ApiContext', () => ({
  useApi: () => ({ connectWorkbenchEvents, getVaultRequests }),
}));

const request = { id: 'vr_1', request_type: 'access', session_id: 'ses_1', card: null };

afterEach(() => {
  handlers.length = 0;
  vi.clearAllMocks();
  getVaultRequests.mockReset();
});

describe('usePendingVaultRequests', () => {
  it('drops rows read under the old authorization and reads them again', async () => {
    let answerReread: (value: { requests: unknown[] }) => void = () => undefined;
    getVaultRequests.mockResolvedValue({ requests: [request] });

    const { result } = renderHook(() => usePendingVaultRequests('ses_1'));
    await waitFor(() => expect(result.current.requests).toHaveLength(1));
    // A healthy bridge stops the fallback poll, so only events refresh now.
    act(() => handlers.forEach((h) => h.onEventBridgeStatus?.({ connected: true })));

    await act(async () => undefined);
    const readsBefore = getVaultRequests.mock.calls.length;
    getVaultRequests.mockReturnValueOnce(new Promise((resolve) => { answerReread = resolve; }));

    act(() => handlers.forEach((h) => h.onAuthorizationChanged?.({})));
    expect(result.current.requests).toEqual([]);
    expect(getVaultRequests).toHaveBeenCalledTimes(readsBefore + 1);

    await act(async () => answerReread({ requests: [{ ...request, id: 'vr_2' }] }));
    expect(result.current.requests.map((r) => r.id)).toEqual(['vr_2']);
  });
});
