/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkbenchEventSource } from './workbenchEventSource';

const fetchSession = vi.hoisted(() => vi.fn());
vi.mock('./apiFetch', async (original) => ({
  ...await original<typeof import('./apiFetch')>(),
  apiFetch: fetchSession,
}));

class LegacyStream extends EventTarget {
  static instances: LegacyStream[] = [];
  readyState = 0;
  closed = false;
  constructor(readonly url: string) { super(); LegacyStream.instances.push(this); }
  close() { this.closed = true; this.readyState = 2; }
}

class Socket {
  static instances: Socket[] = [];
  readyState = 0;
  closed = false;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly url: URL) { Socket.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  sendFrame(data: string) { this.onmessage?.(new MessageEvent('message', { data })); }
  close() { this.closed = true; this.readyState = 3; this.onclose?.(); }
}
const sources: WorkbenchEventSource[] = [];
const source = (url = '/api/events') => {
  const stream = new WorkbenchEventSource(url);
  sources.push(stream);
  return stream;
};
const session = (body: object, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => {
  vi.stubGlobal('EventSource', LegacyStream);
  vi.stubGlobal('WebSocket', Socket);
  LegacyStream.instances = [];
  Socket.instances = [];
  fetchSession.mockReset().mockImplementation(async () => session({ remote: false, event_stream_transport: 'websocket-sse-v1' }));
});
afterEach(() => {
  for (const stream of sources.splice(0)) stream.close();
  vi.unstubAllGlobals();
});

describe('workbench transport negotiation', () => {
  it('chooses a socket only from the Runtime capability and preserves named frames', async () => {
    const stream = source();
    const events: MessageEvent[] = [];
    stream.addEventListener('message.new', (event) => events.push(event as MessageEvent));
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1));
    const socket = Socket.instances[0];
    expect(socket.url.protocol).toBe('ws:');
    expect(LegacyStream.instances).toHaveLength(0);
    socket.open();
    expect(stream.readyState).toBe(1);
    socket.sendFrame(': ping\n\nid: row1\nevent: message.new\ndata: {"text":');
    socket.sendFrame('"中文"}\n\nid: row2\r\nevent: message.new\r\ndata: second\r\n\r\n');
    expect(events.map((event) => [event.data, event.lastEventId])).toEqual([
      ['{"text":"中文"}', 'row1'], ['second', 'row2'],
    ]);
  });

  it('uses the old Runtime SSE subscription with the same listeners', async () => {
    fetchSession.mockResolvedValue(session({ remote: false }));
    const stream = source();
    const received = vi.fn();
    stream.addEventListener('connected', received);
    await vi.waitFor(() => expect(LegacyStream.instances).toHaveLength(1));
    const legacy = LegacyStream.instances[0];
    legacy.readyState = 1;
    legacy.dispatchEvent(new MessageEvent('connected', { data: '{"sub_id":1}' }));
    expect(received).toHaveBeenCalledOnce();
    expect(stream.readyState).toBe(1);
    stream.close();
    expect(legacy.closed).toBe(true);
  });

  it.each([
    [{ remote: true, authenticated: false }, 200],
    [{ remote: true, authenticated: true, authorization_state: 'revoked' }, 200],
    [{ remote: true, authenticated: true, authorization_state: 'unavailable' }, 200],
    [{ remote: false, event_stream_transport: 'unknown-version' }, 200],
    [{}, 200], [{ error: 'denied' }, 403], [{ error: 'unavailable' }, 503],
  ])('never downgrades failed or invalid session admission: %j', async (body, status) => {
    fetchSession.mockResolvedValue(session(body, status));
    const stream = source();
    const error = vi.fn();
    stream.onerror = error;
    await vi.waitFor(() => expect(error).toHaveBeenCalledOnce());
    expect(Socket.instances).toHaveLength(0);
    expect(LegacyStream.instances).toHaveLength(0);
  });

  it('leaves reconnect to ApiContext and does not downgrade a rejected socket', async () => {
    const stream = source();
    const error = vi.fn();
    stream.onerror = error;
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1));
    Socket.instances[0].onerror?.();
    Socket.instances[0].onclose?.();
    expect(error).toHaveBeenCalledOnce();
    expect(LegacyStream.instances).toHaveLength(0);
    expect(Socket.instances).toHaveLength(1);
  });

  it('cancels negotiation when closed and does not open a late socket', async () => {
    let resolve!: (value: Response) => void;
    fetchSession.mockImplementation(() => new Promise<Response>((done) => { resolve = done; }));
    const stream = source();
    stream.close();
    expect(fetchSession.mock.calls[0][1].signal.aborted).toBe(true);
    resolve(session({ remote: false, event_stream_transport: 'websocket-sse-v1' }));
    await new Promise((done) => setTimeout(done, 0));
    expect(Socket.instances).toHaveLength(0);
  });

  it('releases the old instance socket and rejects its late frames after switching origins', async () => {
    const first = source('https://first.example.test/api/events');
    const stale = vi.fn();
    first.addEventListener('message.new', stale);
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(1));
    first.close();
    const second = source('https://second.example.test/api/events');
    const received = vi.fn();
    second.addEventListener('message.new', received);
    await vi.waitFor(() => expect(Socket.instances).toHaveLength(2));
    expect(Socket.instances[0].closed).toBe(true);
    expect(Socket.instances[1].url.href).toBe('wss://second.example.test/api/events');
    Socket.instances[0].sendFrame('event: message.new\ndata: stale\n\n');
    Socket.instances[1].open();
    Socket.instances[1].sendFrame('event: message.new\ndata: current\n\n');
    expect(stale).not.toHaveBeenCalled();
    expect(received).toHaveBeenCalledOnce();
  });
});
