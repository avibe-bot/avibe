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

  it('hears on each answer how that request arrived', async () => {
    const answers = ['other', 'direct', null];
    window.fetch = vi.fn(async () => {
      const word = answers.shift();
      return new Response(null, { status: 204, headers: word ? { 'X-Avibe-Via': word } : {} });
    }) as typeof window.fetch;
    const connections = await import('./pageConnections');
    const page = await import('./liveStreamVia');
    connections.installPageFetchCounting();
    expect(page.latestRequestVia()).toBeNull();
    await window.fetch('/api/session');
    expect(page.latestRequestVia()).toBe('other');
    await window.fetch('/api/inbox');
    expect(page.latestRequestVia()).toBe('direct');
    // An answer that does not say leaves the last word standing.
    await window.fetch('/assets/app.js');
    expect(page.latestRequestVia()).toBe('direct');
  });

  it('does not take another origin\'s answer for the instance\'s word', async () => {
    window.fetch = vi.fn(async () => new Response(null, { status: 204, headers: { 'X-Avibe-Via': 'direct' } })) as typeof window.fetch;
    const connections = await import('./pageConnections');
    const page = await import('./liveStreamVia');
    connections.installPageFetchCounting();
    await window.fetch('https://elsewhere.example/api/session');
    expect(page.latestRequestVia()).toBeNull();
  });
});
