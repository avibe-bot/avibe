import { describe, expect, it } from 'vitest';

import { consoleOriginAfterSave, isListenableHost } from './consoleServerOrigin';

const at = (hostname: string, port = '5123') => ({ hostname, port });

describe('where the page is after the console server host or port is saved', () => {
  it.each([
    // On the computer that runs Avibe, a network host never takes the page off loopback.
    ['127.0.0.1', '192.168.1.20', 5123, 'http://127.0.0.1:5123', 'stay'],
    ['localhost', '192.168.1.20', 5123, 'http://localhost:5123', 'stay'],
    ['127.0.0.1', '192.168.1.20', 6000, 'http://127.0.0.1:6000', 'go'],
    // The loopback listener is of the saved host's family.
    ['127.0.0.1', 'fd00::4', 5123, 'http://[::1]:5123', 'go'],
    ['[::1]', 'fd00::4', 5123, 'http://[::1]:5123', 'stay'],
    ['localhost', 'fd00::4', 5123, 'http://localhost:5123', 'stay'],
    // Between loopback addresses, and for a bind to every address, as before.
    ['127.0.0.1', '::1', 5123, 'http://[::1]:5123', 'go'],
    ['127.0.0.1', '0.0.0.0', 5123, 'http://127.0.0.1:5123', 'stay'],
    ['127.0.0.1', '0.0.0.0', 6000, 'http://127.0.0.1:6000', 'go'],
    // From another device (an unbound Avibe): it follows a network host, and is told a loopback one.
    ['192.168.1.20', '192.168.1.30', 5123, 'http://192.168.1.30:5123', 'go'],
    ['192.168.1.20', '127.0.0.1', 5123, 'http://127.0.0.1:5123', 'tell'],
    ['192.168.1.20', '0.0.0.0', 5123, 'http://192.168.1.20:5123', 'stay'],
    // `*` is every address too: never an address to go to.
    ['192.168.1.20', '*', 5123, 'http://192.168.1.20:5123', 'stay'],
    ['127.0.0.1', '*', 6000, 'http://127.0.0.1:6000', 'go'],
  ] as const)('page at %s, host saved as %s:%i', (hostname, host, port, origin, action) => {
    expect(consoleOriginAfterSave(at(hostname), { host, port })).toEqual({ origin, action });
  });

  it.each([
    ['192.168.1.20', 5123],
    ['127.0.0.1', 6000],
  ])('a page opened by name (signed in at the Avibe\'s own address) is not sent to %s', (host, port) => {
    expect(consoleOriginAfterSave(at('alex.avibe.bot', '443'), { host, port }).action).toBe('stay');
  });
});

describe('a host the console server can listen on', () => {
  it.each(['127.0.0.1', '0.0.0.0', '192.168.1.20', '::', '::1', '[::1]', 'fd00::4', '*', 'localhost', 'LOCALHOST',
    'avibe.local', 'my-mac', ' 192.168.1.20 '])('accepts %s', (host) => {
    expect(isListenableHost(host)).toBe(true);
  });

  it.each(['', '192.168.1', '192.168.1.256', '1.2.3.4.5', 'fe80::1%en0', ':::', 'bad host', 'a_b.local', '-a.local',
    "x'); import os #", 'http://192.168.1.20', '192.168.1.20:5123', 'name.123'])('refuses %s', (host) => {
    expect(isListenableHost(host)).toBe(false);
  });
});
