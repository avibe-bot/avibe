/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// xterm draws to a canvas jsdom does not have. The terminal's rendering is not
// under test here; its socket lifecycle is.
vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    options: Record<string, unknown> = {};
    loadAddon() {}
    attachCustomKeyEventHandler() {}
    open() {}
    onData() {
      return { dispose() {} };
    }
    write() {}
    paste() {}
    focus() {}
    dispose() {}
  },
}));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
vi.mock('@xterm/addon-search', () => ({
  SearchAddon: class {
    onDidChangeResults() {
      return { dispose() {} };
    }
    clearDecorations() {}
  },
}));
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: class {} }));
vi.mock('@xterm/xterm/css/xterm.css', () => ({}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
// Authorization closes hand recovery to the sign-in flow, which navigates.
vi.mock('@/lib/remoteAuth', () => ({
  REMOTE_AUTH_STATE_EVENT: 'avibe:test:remote-auth-state',
  reportRemoteAuthorizationState: vi.fn(),
}));

import { TerminalView } from './TerminalView';

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.OPEN;
  binaryType = '';
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(): void {}

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  /** A control frame from the server. */
  frame(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) } as MessageEvent);
  }

  /** The connection ended without the client asking. */
  drop(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }

  static latest(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (!socket) throw new Error('no WebSocket was opened');
    return socket;
  }
}

/** The native shell reporting that this origin moved to another path. */
let changeTransport: () => void;

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  let deliver: (payload: unknown) => void = () => {};
  let sequence = 0;
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: { direct: 1 },
      call: vi.fn(async () => null),
      on: (event: string, handler: (payload: unknown) => void) => {
        if (event === 'direct.transportChanged') deliver = handler;
        return () => undefined;
      },
    },
  });
  changeTransport = () => {
    sequence += 1;
    act(() => deliver({ transport: sequence % 2 ? 'direct' : 'tunnel', sequence }));
  };
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, 'avibeNative');
  vi.unstubAllGlobals();
});

const mountTerminal = () => {
  render(<TerminalView sessionId="term-1" />);
  return FakeWebSocket.latest();
};

describe('TerminalView on a native transport change', () => {
  it('reattaches a persistent session, live or already lost', () => {
    const first = mountTerminal();
    act(() => first.frame({ type: 'ready', persistent: true }));

    changeTransport();
    expect(first.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // The direct path went away under the replacement: its socket is gone
    // before the shell reports the change.
    const second = FakeWebSocket.latest();
    act(() => second.frame({ type: 'ready', persistent: true }));
    act(() => second.drop(1006));
    changeTransport();
    expect(FakeWebSocket.instances).toHaveLength(3);
  });

  it('reconnects a socket that never attached', () => {
    const connecting = mountTerminal();

    changeTransport();
    expect(connecting.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('leaves a plain shell alone: its process dies with its socket', () => {
    const live = mountTerminal();
    act(() => live.frame({ type: 'ready', persistent: false }));

    changeTransport();
    expect(live.closed).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);

    // Once lost, a silent new shell would hide that the process is gone.
    act(() => live.drop(1006));
    changeTransport();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it.each([
    ['the shell exited', (socket: FakeWebSocket) => socket.frame({ type: 'exit', code: 0 })],
    ['the terminal is disabled', (socket: FakeWebSocket) => socket.drop(1008)],
    ['access was revoked', (socket: FakeWebSocket) => socket.drop(4403)],
    ['sign-in is required', (socket: FakeWebSocket) => socket.drop(4401)],
    ['authorization is unavailable', (socket: FakeWebSocket) => socket.drop(4503)],
  ])('does not reconnect when %s', (_label, settle) => {
    const socket = mountTerminal();
    act(() => socket.frame({ type: 'ready', persistent: true }));
    act(() => settle(socket));

    changeTransport();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
