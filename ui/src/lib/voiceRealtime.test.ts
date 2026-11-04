import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  encodeVoiceRealtimePcm,
  parseVoiceRealtimeMessage,
  VoiceRealtimeSession,
  VOICE_REALTIME_PROTOCOL,
} from './voiceRealtime';

class FakeSocket {
  readyState = 0;
  autoReady = true;
  autoFinal = true;
  sent: string[] = [];
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>();

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  send(value: string): void {
    this.sent.push(value);
    const message = JSON.parse(value) as { type?: string };
    if (message.type === 'start' && this.autoReady) {
      this.emit('message', { data: JSON.stringify({ type: 'ready', protocol: VOICE_REALTIME_PROTOCOL }) });
    }
    if (message.type === 'finish' && this.autoFinal) {
      this.emit('message', {
        data: JSON.stringify({ type: 'final', text: '最终文本', cleanup: 'success' }),
      });
    }
  }

  close(): void {
    this.readyState = 3;
    this.emit('close', {});
  }

  open(): void {
    this.readyState = 1;
    this.emit('open', {});
  }

  emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

const connect = (socket: FakeSocket, capabilities: string[] = []) =>
  async () => ({ socket: socket as unknown as WebSocket, capabilities });

const startFrame = (socket: FakeSocket): Record<string, unknown> =>
  JSON.parse(socket.sent.find((frame) => JSON.parse(frame).type === 'start') ?? 'null');

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('voice realtime client protocol', () => {
  it('validates server events and keeps the final text distinct from preview', () => {
    expect(parseVoiceRealtimeMessage(JSON.stringify({
      type: 'preview',
      text: '临时',
      stash: '尾部',
    }))).toEqual({ type: 'preview', text: '临时', stash: '尾部' });
    expect(parseVoiceRealtimeMessage(JSON.stringify({
      type: 'final',
      text: '最终',
      cleanup: 'success',
    }))).toEqual({ type: 'final', text: '最终', cleanup: 'success' });
    expect(parseVoiceRealtimeMessage(JSON.stringify({ type: 'ready', protocol: 'other' }))).toBeNull();
  });

  it('encodes little-endian PCM as base64', () => {
    expect(encodeVoiceRealtimePcm(new Int16Array([0x0102, -2]))).toBe('AgH+/w==');
  });

  it('buffers the first PCM chunks until the upstream handshake is ready', async () => {
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const socket = new FakeSocket();
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      openSocket: connect(socket),
    });
    const start = session.start();
    expect(session.sendPcm(new Int16Array([1]))).toBe(true);
    socket.open();
    await start;
    expect(socket.sent.some((frame) => JSON.parse(frame).type === 'audio')).toBe(true);
    await expect(session.finish()).resolves.toEqual({
      text: '最终文本',
      cleanup: 'success',
    });
  });

  it('preserves a socket failure that happens before finish', async () => {
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const socket = new FakeSocket();
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      openSocket: connect(socket),
    });
    const start = session.start();
    socket.open();
    await start;

    socket.close();

    await expect(session.finish()).rejects.toThrow('realtime_closed');
  });

  it('closes a socket when the handshake deadline expires', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', { OPEN: 1, CLOSED: 3 });
    const socket = new FakeSocket();
    socket.autoReady = false;
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      openSocket: connect(socket),
    });
    const start = session.start();
    await Promise.resolve();
    socket.open();
    const expectation = expect(start).rejects.toThrow('realtime_timeout');
    await vi.advanceTimersByTimeAsync(8_000);

    await expectation;
    expect(socket.readyState).toBe(3);
  });

  it('reports an active socket failure and stops accepting PCM', async () => {
    vi.stubGlobal('WebSocket', { OPEN: 1, CLOSED: 3 });
    const socket = new FakeSocket();
    const onError = vi.fn();
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      onError,
      openSocket: connect(socket),
    });
    const start = session.start();
    socket.open();
    await start;

    socket.close();

    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: 'realtime_closed' }));
    expect(session.sendPcm(new Int16Array([1]))).toBe(false);
  });

  it('rejects the open phase when the socket fails before opening', async () => {
    vi.stubGlobal('WebSocket', { OPEN: 1, CLOSED: 3 });
    const socket = new FakeSocket();
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      openSocket: connect(socket),
    });
    const start = session.start();
    await Promise.resolve();
    socket.emit('error', {});

    await expect(start).rejects.toThrow('realtime_socket_error');
    expect(socket.readyState).toBe(3);
  });

  it('closes the socket when finalization times out', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', { OPEN: 1, CLOSED: 3 });
    const socket = new FakeSocket();
    socket.autoFinal = false;
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      openSocket: connect(socket),
    });
    const start = session.start();
    await Promise.resolve();
    socket.open();
    await start;
    const finish = session.finish();
    const expectation = expect(finish).rejects.toThrow('realtime_timeout');

    await vi.advanceTimersByTimeAsync(20_000);

    await expectation;
    expect(socket.readyState).toBe(3);
  });

  it.each([
    ['a backend that declares both', ['realtime_reply_context', 'realtime_user_previous_context'], { reply: '先跑 make release', user_previous: '部署前要做什么' }],
    ['a backend that declares reply context only', ['realtime_reply_context'], { reply: '先跑 make release' }],
    ['a backend that declares the user\'s previous message only', ['realtime_user_previous_context'], { user_previous: '部署前要做什么' }],
    ['a backend without either capability', [], {}],
  ])('sends each context text in start only for %s', async (_case, capabilities, expected) => {
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const socket = new FakeSocket();
    const session = new VoiceRealtimeSession({
      before: '前文',
      after: '后文',
      context: { reply: '先跑 make release', userPrevious: '部署前要做什么' },
      openSocket: connect(socket, capabilities),
    });
    const start = session.start();
    socket.open();
    await start;

    expect(startFrame(socket)).toEqual({ type: 'start', before: '前文', after: '后文', ...expected });
  });

  it.each([
    ['a backend that declares reply context (it has accepted `vocabulary` since before)', ['realtime_reply_context'], true],
    ['a backend that declares nothing', [], false],
  ])('sends the names to hear exactly in start only for %s', async (_case, capabilities, included) => {
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const socket = new FakeSocket();
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      context: { vocabulary: ['歪比', 'Product'] },
      openSocket: connect(socket, capabilities),
    });
    const start = session.start();
    socket.open();
    await start;

    expect(startFrame(socket)).toEqual({ type: 'start', before: '', after: '', ...(included ? { vocabulary: ['歪比', 'Product'] } : {}) });
  });

  it('sends empty context texts as nothing', async () => {
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const socket = new FakeSocket();
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      context: { reply: '  ', userPrevious: '' },
      openSocket: connect(socket, ['realtime_reply_context', 'realtime_user_previous_context']),
    });
    const start = session.start();
    socket.open();
    await start;

    expect(startFrame(socket)).toEqual({ type: 'start', before: '', after: '' });
  });

  it('cuts each context text to its newest 8,000 characters', async () => {
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const socket = new FakeSocket();
    const reply = `${'旧'.repeat(10)}${'x'.repeat(8_000)}`;
    const userPrevious = `${'早'.repeat(5)}${'y'.repeat(7_999)}末`;
    const session = new VoiceRealtimeSession({
      before: '',
      after: '',
      context: { reply, userPrevious },
      openSocket: connect(socket, ['realtime_reply_context', 'realtime_user_previous_context']),
    });
    const start = session.start();
    socket.open();
    await start;

    expect(startFrame(socket).reply).toBe('x'.repeat(8_000));
    expect(startFrame(socket).user_previous).toBe(`${'y'.repeat(7_999)}末`);
  });

  it('waits for a context still loading, and starts without one that does not come', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', { OPEN: 1 });
    const loaded = new FakeSocket();
    let resolveContext: (context: { reply: string }) => void = () => undefined;
    const loading = new VoiceRealtimeSession({
      before: '',
      after: '',
      context: new Promise((resolve) => { resolveContext = resolve; }),
      openSocket: connect(loaded, ['realtime_reply_context']),
    });
    const first = loading.start();
    loaded.open();
    await vi.advanceTimersByTimeAsync(200);
    expect(loaded.sent).toHaveLength(0);
    resolveContext({ reply: '刚到的回复' });
    await first;
    expect(startFrame(loaded).reply).toBe('刚到的回复');

    const stalled = new FakeSocket();
    const late = new VoiceRealtimeSession({
      before: '',
      after: '',
      context: new Promise(() => undefined),
      openSocket: connect(stalled, ['realtime_reply_context']),
    });
    const second = late.start();
    stalled.open();
    await vi.advanceTimersByTimeAsync(1_500);
    await second;
    expect(startFrame(stalled)).toEqual({ type: 'start', before: '', after: '' });
  });
});
