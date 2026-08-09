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
      openSocket: async () => socket as unknown as WebSocket,
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
      openSocket: async () => socket as unknown as WebSocket,
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
      openSocket: async () => socket as unknown as WebSocket,
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
      openSocket: async () => socket as unknown as WebSocket,
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
      openSocket: async () => socket as unknown as WebSocket,
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
      openSocket: async () => socket as unknown as WebSocket,
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
});
