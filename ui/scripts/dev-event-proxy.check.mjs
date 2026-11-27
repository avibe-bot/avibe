import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import http from 'node:http';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createServer } from 'vite';

test('the real Vite dev proxy forwards event upgrades with the backend Origin', async () => {
  const sockets = new Set();
  const seen = [];
  let backendOrigin;
  const backend = http.createServer((_request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ remote: false, event_stream_transport: 'websocket-sse-v1' }));
  });
  backend.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  backend.on('upgrade', (request, socket) => {
    seen.push({ origin: request.headers.origin, host: request.headers.host, path: request.url });
    if (request.headers.origin !== backendOrigin || request.headers.host !== new URL(backendOrigin).host) {
      socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    const accept = createHash('sha1').update(`${request.headers['sec-websocket-key']}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const body = Buffer.from('event: connected\ndata: {}\n\n');
    socket.write(Buffer.concat([Buffer.from([0x81, body.length]), body]));
    socket.on('data', data => { if ((data[0] & 15) === 8) socket.end(Buffer.from([0x88, 0])); });
  });
  backend.listen(0, '127.0.0.1');
  await once(backend, 'listening');
  backendOrigin = `http://127.0.0.1:${backend.address().port}`;
  const previous = process.env.VIBE_UI_BACKEND;
  process.env.VIBE_UI_BACKEND = backendOrigin;
  let vite;
  try {
    vite = await createServer({
      root: fileURLToPath(new URL('../', import.meta.url)),
      configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
      server: { port: 0, host: '127.0.0.1', open: false }, logLevel: 'error',
    });
    await vite.listen();
    const origin = `http://127.0.0.1:${vite.httpServer.address().port}`;
    const capability = await (await fetch(`${origin}/api/session`)).json();
    assert.equal(capability.event_stream_transport, 'websocket-sse-v1');
    const socket = new WebSocket(`${origin.replace('http:', 'ws:')}/api/events`);
    try {
      const text = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Vite did not forward event upgrade')), 5000);
        socket.onmessage = event => { clearTimeout(timer); resolve(event.data); };
        socket.onerror = () => { clearTimeout(timer); reject(new Error('Event upgrade rejected')); };
      });
      assert.equal(text, 'event: connected\ndata: {}\n\n');
      assert.deepEqual(seen, [{ origin: backendOrigin, host: new URL(backendOrigin).host, path: '/api/events' }]);
    } finally { socket.close(); }
  } finally {
    for (const socket of sockets) socket.destroy();
    await vite?.close();
    await new Promise(resolve => backend.close(resolve));
    if (previous === undefined) delete process.env.VIBE_UI_BACKEND;
    else process.env.VIBE_UI_BACKEND = previous;
  }
});
