import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import type { AddressInfo, Socket } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const buildRoot = resolve(dirname(fileURLToPath(import.meta.url)), '.build');
export const provenance = JSON.parse(readFileSync(resolve(buildRoot, 'provenance.json'), 'utf8')) as Record<string, string>;
export type Runtime = 'old' | 'new';

function frame(text: string): Buffer {
  const payload = Buffer.from(text);
  if (payload.length < 126) return Buffer.concat([Buffer.from([0x81, payload.length]), payload]);
  const header = Buffer.alloc(4);
  header[0] = 0x81;
  header[1] = 126;
  header.writeUInt16BE(payload.length, 2);
  return Buffer.concat([header, payload]);
}

export async function startFixture(runtime: Runtime, options: { denyWs?: boolean; sessionDelayMs?: number } = {}) {
  const responses = new Set<http.ServerResponse>();
  const websockets = new Set<Socket>();
  const connections = new Set<Socket>();
  const requests: Array<{ path: string; kind: string; at: number; version: string; afterId: string | null }> = [];
  let wsOpened = 0;
  let sseOpened = 0;
  let origin = '';
  const record = (request: http.IncomingMessage, kind: string) => {
    const url = new URL(request.url ?? '/', origin);
    requests.push({ path: `${url.pathname}${url.search}`, kind, at: performance.now(), version: request.httpVersion, afterId: url.searchParams.get('after_id') });
  };
  const server = http.createServer((request, response) => {
    const url = new URL(request.url ?? '/', origin);
    response.setHeader('Cache-Control', 'no-store');
    // Two test-owned origins exercise switching instances. This is a transport
    // fixture; real Runtime admission/authorization has separate integration tests.
    response.setHeader('Access-Control-Allow-Origin', '*');
    if (url.pathname === '/api/session') {
      record(request, 'session');
      const answer = () => {
        if (response.destroyed) return;
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ remote: false, ...(runtime === 'new' ? { event_stream_transport: 'websocket-sse-v1' } : {}) }));
      };
      if (options.sessionDelayMs) {
        const timer = setTimeout(answer, options.sessionDelayMs);
        response.on('close', () => clearTimeout(timer));
      } else answer();
      return;
    }
    if (url.pathname.endsWith('/events')) {
      record(request, 'sse');
      sseOpened += 1;
      responses.add(response);
      response.writeHead(200, { 'Content-Type': 'text/event-stream', Connection: 'keep-alive' });
      response.write(': fixture connected\n\n');
      response.on('close', () => responses.delete(response));
      return;
    }
    if (url.pathname === '/probe') {
      record(request, 'fetch');
      response.end('ordinary HTTP/1.1 fetch');
      return;
    }
    if (url.pathname === '/fixture.js') {
      response.writeHead(200, { 'Content-Type': 'text/javascript' });
      response.end(readFileSync(resolve(buildRoot, 'fixture.js')));
      return;
    }
    record(request, 'document');
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end(`<!doctype html><meta charset="utf-8"><title>Isolated event transport</title>
<script>globalThis.__AVIBE_SHOW__=${JSON.stringify({
  basePath: '/show/fixture/',
  ...(runtime === 'new' ? { eventStreamTransport: 'websocket-sse-v1' } : {}),
})}</script><script src="/fixture.js"></script>`);
  });
  server.on('connection', (socket) => {
    connections.add(socket);
    socket.on('close', () => connections.delete(socket));
  });
  server.on('upgrade', (request, socket) => {
    record(request, 'upgrade');
    if (runtime === 'old' || options.denyWs) {
      socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    const key = request.headers['sec-websocket-key'];
    if (typeof key !== 'string') { socket.destroy(); return; }
    const accept = createHash('sha1').update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const stream = socket as Socket;
    websockets.add(stream);
    wsOpened += 1;
    stream.on('close', () => websockets.delete(stream));
    stream.on('error', () => { /* Test clients may abort a handshake/stream. */ });
    stream.on('data', (data: Buffer) => {
      if ((data[0] & 0x0f) === 8) stream.end(Buffer.from([0x88, 0]));
    });
    stream.write(frame(': fixture connected\n\n'));
  });
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
  origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return {
    origin,
    requests,
    stats: () => ({ activeSse: responses.size, activeWs: websockets.size, sseOpened, wsOpened }),
    send(text: string, fragments = false) {
      for (const socket of websockets) {
        if (fragments) for (const character of text) socket.write(frame(character));
        else socket.write(frame(text));
      }
      for (const response of responses) response.write(text);
    },
    dropWs() { for (const socket of websockets) socket.destroy(); },
    async close() {
      for (const socket of connections) socket.destroy();
      await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done()));
    },
  };
}

export type Fixture = Awaited<ReturnType<typeof startFixture>>;
