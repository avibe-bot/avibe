/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

const originalFetch = window.fetch;

afterEach(() => {
  window.fetch = originalFetch;
  vi.resetModules();
});

describe('page connections', () => {
  it('keeps no socket where no hand-over can happen', async () => {
    const connections = await import('./pageConnections');
    const socket = { readyState: 1, addEventListener: vi.fn() } as unknown as WebSocket;
    connections.trackPageSocket(socket);
    expect(connections.pageConnectionsQuiet()).toBe(true);
    expect(socket.addEventListener).not.toHaveBeenCalled();
  });

  it('forgets a socket when it closes', async () => {
    window.fetch = vi.fn(async () => new Response(null, { status: 204 })) as typeof window.fetch;
    const connections = await import('./pageConnections');
    connections.installPageFetchCounting();
    let onClose: () => void = () => {};
    const socket = {
      readyState: 1,
      addEventListener: (_type: string, listener: () => void) => { onClose = listener; },
    } as unknown as WebSocket;
    connections.trackPageSocket(socket);
    expect(connections.pageConnectionsQuiet()).toBe(false);
    onClose();
    expect(connections.pageConnectionsQuiet()).toBe(true);
  });
});
