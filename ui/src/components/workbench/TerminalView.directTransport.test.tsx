/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// xterm draws to a canvas jsdom does not have. The terminal's rendering is not
// under test here; its socket lifecycle is.
// The most recent terminal's input callback: what a keystroke reaches.
const typed = vi.hoisted(() => ({ current: null as ((data: string) => void) | null }));
vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    options: Record<string, unknown> = {};
    loadAddon() {}
    attachCustomKeyEventHandler() {}
    open() {}
    onData(callback: (data: string) => void) {
      typed.current = callback;
      return {
        dispose() {
          if (typed.current === callback) typed.current = null;
        },
      };
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
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  // A new socket is still connecting, as a real one is.
  readyState = FakeWebSocket.CONNECTING;
  readonly sent: string[] = [];
  binaryType = '';
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: Uint8Array): void {
    this.sent.push(new TextDecoder().decode(data));
  }

  /** The connection opened. */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

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

  it('drops what is typed while the replacement connects, and sends what is typed after', async () => {
    const first = await mountTerminal();
    act(() => first.open());
    act(() => first.frame({ type: 'ready', persistent: true }));
    act(() => typed.current?.('before'));
    expect(first.sent).toEqual(['before']);

    closeLostPath();
    const second = FakeWebSocket.latest();
    expect(second).not.toBe(first);
    expect(second.readyState).toBe(FakeWebSocket.CONNECTING);
    // Typed in the gap: no socket is open, so it goes nowhere.
    act(() => typed.current?.('lost in the gap'));

    act(() => second.open());
    act(() => second.frame({ type: 'ready', persistent: true }));
    expect(first.sent).toEqual(['before']);
    expect(second.sent).toEqual([]);

    // The positive control: input after the reattach is sent.
    act(() => typed.current?.('after'));
    expect(second.sent).toEqual(['after']);
  });

  it('keeps a confirmed persistent session eligible while its replacement has not attached yet', async () => {
    const first = await mountTerminal();
    act(() => first.frame({ type: 'ready', persistent: true }));

    closeLostPath();
    const second = FakeWebSocket.latest();
    // The replacement is lost before its own ready frame.
    act(() => second.drop(1006));
    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(3);

    // A duplicate of that closure reattaches nothing more.
    act(() => deliver({ preferred: 'tunnel', sequence, closure }));
    expect(FakeWebSocket.instances).toHaveLength(3);

    // A later closure while the third is still connecting reattaches again.
    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  it('forgets that eligibility when the server says the session is not persistent', async () => {
    const first = await mountTerminal();
    act(() => first.frame({ type: 'ready', persistent: true }));
    closeLostPath();
    // The reattach landed on a plain shell: the session it had is gone.
    act(() => FakeWebSocket.latest().frame({ type: 'ready', persistent: false }));
    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(2);
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

  it('shows its error and opens no socket while the baseline is unavailable; Reconnect asks once', async () => {
    vi.useFakeTimers();
    try {
      vi.resetModules();
      const { TerminalView } = await import('./TerminalView');
      const onStatus = vi.fn();
      render(<TerminalView sessionId="term-1" onStatus={onStatus} />);
      expect(onStatus).toHaveBeenLastCalledWith('connecting', null);

      // The first read never settles.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(FakeWebSocket.instances).toHaveLength(0);
      expect(onStatus).toHaveBeenLastCalledWith('error', null);

      // The user's Reconnect asks for a baseline; a second click while that
      // read is out asks for nothing more.
      const button = screen.getByRole('button', { name: /apps\.terminal\.reconnect/ });
      act(() => fireEvent.click(button));
      expect(statusAnswers).toHaveLength(2);
      expect(onStatus).toHaveBeenLastCalledWith('connecting', null);
      expect(screen.queryByRole('button', { name: /apps\.terminal\.reconnect/ })).toBeNull();
      expect(FakeWebSocket.instances).toHaveLength(0);

      await act(async () => {
        statusAnswers[1]({ preferred: 'tunnel', sequence: 0, closure: 0 });
      });
      expect(FakeWebSocket.instances).toHaveLength(1);
      // The first read answers after all: one baseline, one socket.
      await act(async () => {
        statusAnswers[0]({ preferred: 'tunnel', sequence: 0, closure: 0 });
      });
      expect(FakeWebSocket.instances).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('opens when a held answer arrives after the wait, and opens nothing once unmounted', async () => {
    vi.useFakeTimers();
    try {
      vi.resetModules();
      const { TerminalView } = await import('./TerminalView');
      const view = render(<TerminalView sessionId="term-1" />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(FakeWebSocket.instances).toHaveLength(0);
      await act(async () => {
        statusAnswers[0]({ preferred: 'tunnel', sequence: 0, closure: 0 });
      });
      expect(FakeWebSocket.instances).toHaveLength(1);
      view.unmount();

      // A second page whose terminal is gone before its baseline arrives.
      FakeWebSocket.instances = [];
      statusAnswers = [];
      vi.resetModules();
      const again = await import('./TerminalView');
      const gone = render(<again.TerminalView sessionId="term-2" />);
      gone.unmount();
      await act(async () => {
        statusAnswers[0]({ preferred: 'tunnel', sequence: 0, closure: 0 });
      });
      expect(FakeWebSocket.instances).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('starts a new session without the old one\'s persistence', async () => {
    vi.resetModules();
    const { TerminalView } = await import('./TerminalView');
    const view = render(<TerminalView sessionId="term-1" />);
    await act(async () => {
      statusAnswers[0]({ preferred: 'tunnel', sequence, closure });
    });
    act(() => FakeWebSocket.latest().frame({ type: 'ready', persistent: true }));

    view.rerender(<TerminalView sessionId="term-2" />);
    expect(FakeWebSocket.instances).toHaveLength(2);
    // The new session has not been confirmed persistent: nothing reattaches.
    closeLostPath();
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
