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

/** `direct.status` answers waiting for the test, and the shell's event. */
let statusAnswers: Array<(value: unknown) => void>;
let deliver: (payload: unknown) => void;
let closure: number;
let sequence: number;

const installShell = (capabilities: Record<string, number> = { direct: 1 }) => {
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities,
      call: vi.fn(
        (method: string) =>
          new Promise((resolve) => {
            if (method === 'direct.status') statusAnswers.push(resolve);
            else resolve(null);
          }),
      ),
      on: (event: string, handler: (payload: unknown) => void) => {
        if (event === 'direct.transportChanged') deliver = handler;
        return () => undefined;
      },
    },
  });
};

/** The shell closed connections for this origin because their path was lost. */
const closeLostPath = () => {
  closure += 1;
  sequence += 1;
  act(() => deliver({ preferred: 'tunnel', sequence, closure }));
};

/** The preferred path for new connections changed; nothing was closed. */
const changePreference = () => {
  sequence += 1;
  act(() => deliver({ preferred: sequence % 2 ? 'direct' : 'tunnel', sequence, closure }));
};

beforeEach(() => {
  FakeWebSocket.instances = [];
  statusAnswers = [];
  deliver = () => {};
  closure = 0;
  sequence = 0;
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  installShell();
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, 'avibeNative');
  vi.unstubAllGlobals();
});

/**
 * A new page: a fresh copy of the bridge module, whose direct state belongs
 * to the document, and its baseline answered.
 */
const mountTerminal = async () => {
  vi.resetModules();
  const { TerminalView } = await import('./TerminalView');
  render(<TerminalView sessionId="term-1" />);
  if (statusAnswers.length > 0) {
    await act(async () => {
      statusAnswers[0]({ preferred: 'tunnel', sequence, closure });
    });
  }
  return FakeWebSocket.latest();
};

describe('TerminalView when the native shell closes connections on a lost path', () => {
  it('reattaches a persistent session, live or already lost', async () => {
    const first = await mountTerminal();
    act(() => first.frame({ type: 'ready', persistent: true }));

    closeLostPath();
    expect(first.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // The direct path went away under the replacement: its socket is gone
    // before the shell reports the closure.
    const second = FakeWebSocket.latest();
    act(() => second.frame({ type: 'ready', persistent: true }));
    act(() => second.drop(1006));
    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(3);
  });

  it('does nothing for a change of preferred path alone', async () => {
    const socket = await mountTerminal();
    act(() => socket.frame({ type: 'ready', persistent: true }));

    changePreference();
    changePreference();
    expect(socket.closed).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('leaves a socket that has not attached yet alone', async () => {
    const connecting = await mountTerminal();

    closeLostPath();
    expect(connecting.closed).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('leaves a plain shell alone: its process dies with its socket', async () => {
    const live = await mountTerminal();
    act(() => live.frame({ type: 'ready', persistent: false }));

    closeLostPath();
    expect(live.closed).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(1);

    // Once lost, a silent new shell would hide that the process is gone.
    act(() => live.drop(1006));
    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it.each([
    ['the shell exited', (socket: FakeWebSocket) => socket.frame({ type: 'exit', code: 0 })],
    ['the terminal is disabled', (socket: FakeWebSocket) => socket.drop(1008)],
    ['access was revoked', (socket: FakeWebSocket) => socket.drop(4403)],
    ['sign-in is required', (socket: FakeWebSocket) => socket.drop(4401)],
    ['authorization is unavailable', (socket: FakeWebSocket) => socket.drop(4503)],
  ])('does not reconnect when %s', async (_label, settle) => {
    const socket = await mountTerminal();
    act(() => socket.frame({ type: 'ready', persistent: true }));
    act(() => settle(socket));

    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('sends nothing typed while there was no open socket', async () => {
    const first = await mountTerminal();
    act(() => first.frame({ type: 'ready', persistent: true }));
    const sent = vi.spyOn(FakeWebSocket.prototype, 'send');

    closeLostPath();
    const second = FakeWebSocket.latest();
    act(() => second.frame({ type: 'ready', persistent: true }));
    expect(sent).not.toHaveBeenCalled();
  });

  it('waits for the baseline before its first socket, and not at all without the capability', async () => {
    vi.resetModules();
    const { TerminalView } = await import('./TerminalView');
    render(<TerminalView sessionId="term-1" />);
    expect(FakeWebSocket.instances).toHaveLength(0);
    await act(async () => {
      statusAnswers[0]({ preferred: 'tunnel', sequence: 0, closure: 3 });
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
    cleanup();

    FakeWebSocket.instances = [];
    statusAnswers = [];
    installShell({ app: 1 });
    await mountTerminal();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(statusAnswers).toHaveLength(0);
  });
});
