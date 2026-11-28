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

  it('says what keeps the page busy, the longest-lived first', async () => {
    let finish: (response: Response) => void = () => {};
    window.fetch = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })) as typeof window.fetch;
    const connections = await import('./pageConnections');
    connections.installPageFetchCounting();
    expect(connections.pageConnectionsHeldBy()).toBeNull();

    const pending = window.fetch('/api/inbox');
    await Promise.resolve();
    expect(connections.pageConnectionsHeldBy()).toBe('request');
    const socket = { readyState: 1, addEventListener: vi.fn() } as unknown as WebSocket;
    connections.trackPageSocket(socket);
    expect(connections.pageConnectionsHeldBy()).toBe('socket');
    const frame = document.createElement('iframe');
    frame.setAttribute('src', '/show/s1/');
    document.body.appendChild(frame);
    expect(connections.pageConnectionsHeldBy()).toBe('frame');

    frame.remove();
    expect(connections.pageConnectionsHeldBy()).toBe('socket');
    (socket as { readyState: number }).readyState = 3;
    expect(connections.pageConnectionsHeldBy()).toBe('request');
    finish(new Response(null, { status: 204 }));
    await pending;
    expect(connections.pageConnectionsHeldBy()).toBeNull();
    expect(connections.pageConnectionsQuiet()).toBe(true);
  });

  it('holds new requests until every overlapping hold has ended, whichever ends first', async () => {
    const original = vi.fn(async () => new Response(null, { status: 204 }));
    window.fetch = original as typeof window.fetch;
    const connections = await import('./pageConnections');
    connections.installPageFetchCounting();
    const sent = async () => {
      for (let n = 0; n < 5; n += 1) await Promise.resolve();
      return original.mock.calls.length;
    };

    // A hand-over's hold, then a return's inside it, which ends first.
    const endHandover = connections.holdPageRequests();
    const endReturn = connections.holdPageRequests();
    const first = window.fetch('/api/one');
    expect(await sent()).toBe(0);
    endReturn();
    const second = window.fetch('/api/two');
    expect(await sent()).toBe(0);
    endHandover();
    expect(await sent()).toBe(2);
    await Promise.all([first, second]);

    // The other order, and a hold that opens while a request waits.
    const endA = connections.holdPageRequests();
    const third = window.fetch('/api/three');
    const endB = connections.holdPageRequests();
    endA();
    expect(await sent()).toBe(2);
    endB();
    expect(await sent()).toBe(3);
    await third;
    // Ending one twice ends nothing else.
    const endC = connections.holdPageRequests();
    endB();
    const fourth = window.fetch('/api/four');
    expect(await sent()).toBe(3);
    endC();
    expect(await sent()).toBe(4);
    await fourth;
  });

  it('lets a session read that waited out a hold say how long the hold was', async () => {
    const original = vi.fn(async () => new Response(null, { status: 204 }));
    window.fetch = original as typeof window.fetch;
    const connections = await import('./pageConnections');
    const report = await import('./pageReport');
    const { apiFetch } = await import('./apiFetch');
    connections.installPageFetchCounting();
    const stop = report.setPageWhyReader(() => 'no_path');
    const sentHeader = () => {
      const [, init] = original.mock.calls.at(-1) as unknown as [unknown, RequestInit];
      return { word: new Headers(init.headers).get('X-Avibe-Page'), mode: init.mode };
    };

    // A return: the hold opens, the stream's session read is made at once,
    // and the hold ends when the shell answers.
    const end = connections.holdPageRequests();
    const read = apiFetch('/api/session', { cache: 'no-store' });
    for (let n = 0; n < 5; n += 1) await Promise.resolve();
    expect(original).not.toHaveBeenCalled();
    report.notePageHold(250);
    end();
    await read;
    expect(original).toHaveBeenCalledTimes(1);
    expect(sentHeader()).toEqual({ word: 'no_path;hold=250', mode: 'same-origin' });
    // Said once.
    await apiFetch('/api/session');
    expect(sentHeader()).toEqual({ word: null, mode: undefined });
    stop();
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
