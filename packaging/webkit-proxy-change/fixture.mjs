import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import net from 'node:net';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

// This fixture is intentionally independent of the product listener/controller.
// All listeners are runner-loopback-only. No production hostname or credential.
const out = process.argv[2];
if (!out) throw new Error('usage: node fixture.mjs <output-directory>');
const cases = JSON.parse(fs.readFileSync(new URL('./matrix.json', import.meta.url)));
const lifecycleCases = [
  {id: 'lifecycle-reload', origin: 'h1', proxy: 'h1'},
  {id: 'lifecycle-recreate', origin: 'h1', proxy: 'h1'},
  {id: 'lifecycle-relaunch', origin: 'h1', proxy: 'h1'},
];
const byHost = new Map([...cases, ...lifecycleCases].map(c => [`${c.id}.webkit-proxy.test`, c]));
const key = fs.readFileSync(`${out}/key.pem`);
const cert = fs.readFileSync(`${out}/cert.pem`);
const events = [];
let sequence = 0;
let connectionID = 0;
const proxyConnections = new Map();
const originConnections = new Map();
const proxyModes = new Map();
const upstreamConnections = new Map();
const listening = new Set();
const stalledClients = new Set();
const recordedProcesses = new Set();

function log(event, fields = {}) {
  const entry = { sequence: ++sequence, time: new Date().toISOString(), event, ...fields };
  events.push(entry);
  if (event.endsWith('listening')) listening.add(fields.port);
  fs.appendFileSync(`${out}/fixture.jsonl`, JSON.stringify(entry) + '\n');
  console.log(JSON.stringify(entry));
  return entry;
}

const script = String.raw`
window.probe = {
  page: PAGE_PAYLOAD,
  streams: {}, pending: {},
  async fetch(id, path = '/fetch') {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path + '?id=' + encodeURIComponent(id), {
        cache: 'no-store', signal: controller.signal
      });
      const result = await response.json();
      return {ok: true, ...result};
    } catch (error) { return {ok:false, id, error:String(error)}; }
    finally { clearTimeout(timer); }
  },
  openEvents(id) {
    return new Promise(resolve => {
      const item = { events: [], errors: 0, first: null, identities: {}, source: new EventSource('/events?id=' + encodeURIComponent(id)) };
      this.streams[id] = item;
      let answered = false;
      const finish = value => { if (!answered) { answered = true; clearTimeout(timer); resolve(value); } };
      const timer = setTimeout(() => finish({ok:false, id, error:'SSE first-event timeout'}), 10000);
      item.source.onmessage = event => {
        const value = JSON.parse(event.data);
        item.first ??= value;
        const identity = value.connection + ':' + value.stream;
        item.identities[identity] = (item.identities[identity] ?? 0) + 1;
        item.events.push(value);
        finish({ok:true, ...value});
      };
      item.source.onerror = () => { item.errors += 1; };
    });
  },
  closeEvents(id) { this.streams[id]?.source.close(); return this.streamSnapshot(id); },
  streamSnapshot(id) {
    const item = this.streams[id];
    return item ? {ok:true, id, count:item.events.length, errors:item.errors, first:item.first,
      originalCount:item.first ? (item.identities[item.first.connection + ':' + item.first.stream] ?? 0) : 0,
      identities:item.identities, last:item.events.slice(-3)} : {ok:false, id, error:'stream missing'};
  },
  webSocket(id) {
    return new Promise(resolve => {
      const socket = new WebSocket('wss://' + location.host + '/ws?id=' + encodeURIComponent(id));
      let done = false;
      const finish = value => { if (!done) { done = true; clearTimeout(timer); socket.close(); resolve(value); } };
      const timer = setTimeout(() => finish({ok:false, id, error:'WebSocket timeout'}), 10000);
      socket.onmessage = event => finish({ok:true, ...JSON.parse(event.data)});
      socket.onerror = () => finish({ok:false, id, error:'WebSocket error'});
      socket.onclose = event => finish({ok:false, id, error:'WebSocket closed ' + event.code});
    });
  },
  startSlow(id) { this.pending[id] = this.fetch(id, '/slow'); return {ok:true, id, started:true}; },
  async finishSlow(id) { return await this.pending[id]; },
  cleanup() { for (const item of Object.values(this.streams)) item.source.close(); return {ok:true}; }
};
`;

function makeOrigin(route, protocol, host, port) {
  const server = protocol === 'h2'
    ? http2.createSecureServer({ key, cert, allowHTTP1: true })
    : https.createServer({ key, cert });
  const sockets = new WeakMap();
  const sessions = new WeakMap();
  const socketIDs = new Map();
  const idFor = (object, kind) => {
    const map = kind === 'session' ? sessions : sockets;
    if (!map.has(object)) map.set(object, `${route}-${protocol}-${++connectionID}`);
    return map.get(object);
  };
  server.on('secureConnection', socket => {
    const connection = idFor(socket, 'socket');
    const socketKey = `${socket.remotePort}:${socket.localPort}`;
    socketIDs.set(socketKey, connection);
    socket.on('close', () => socketIDs.delete(socketKey));
    if (route === 'origin' && socket.servername) {
      const connections = originConnections.get(socket.servername) ?? new Set();
      connections.add(socket);
      originConnections.set(socket.servername, connections);
      socket.once('close', () => {
        connections.delete(socket);
        if (connections.size === 0) originConnections.delete(socket.servername);
      });
    }
    log('tls_open', {route, protocol, connection, alpn:socket.alpnProtocol, sni:socket.servername, port,
      peerPort:socket.remotePort, proxyConnection:upstreamConnections.get(socketKey) ?? null});
    socket.on('error', error => log('tls_error', {route, connection, error:String(error)}));
    socket.on('close', () => log('tls_close', {route, connection}));
  });
  server.on('session', session => {
    const connection = idFor(session, 'session');
    log('h2_session', {route, protocol, connection});
    session.on('error', error => log('h2_error', {route, connection, error:String(error)}));
    session.on('close', () => log('h2_close', {route, connection}));
  });
  function describe(req) {
    const url = new URL(req.url, `https://${req.headers[':authority'] || req.headers.host}`);
    const hostName = url.hostname;
    const fixture = byHost.get(hostName);
    const connection = req.httpVersionMajor === 2
      ? idFor(req.stream.session, 'session') : idFor(req.socket, 'socket');
    const socketKey = `${req.socket.remotePort}:${req.socket.localPort}`;
    return {route, protocol:req.httpVersion, alpn:req.socket.alpnProtocol, connection,
      tcpConnection:socketIDs.get(socketKey) ?? idFor(req.socket, 'socket'),
      proxyConnection:upstreamConnections.get(socketKey) ?? null,
      stream:req.stream?.id ?? null, case:fixture?.id ?? null, host:hostName,
      path:url.pathname, id:url.searchParams.get('id') ?? '', method:req.method};
  }
  server.on('request', (req, res) => {
    const details = describe(req);
    log('request', details);
    res.on('error', error => log('response_error', {...details, error:String(error)}));
    res.on('finish', () => log('response_finished', details));
    res.on('close', () => log('response_closed', {...details, finished:res.writableFinished}));
    if (!details.case) { res.writeHead(403); res.end(); return; }
    res.setHeader('cache-control', 'no-store');
    if (details.path === '/events') {
      res.setHeader('content-type', 'text/event-stream');
      res.writeHead(200);
      let tick = 0;
      const send = () => {
        const value = {...details, tick:++tick};
        log('sse_tick', value);
        res.write(`data: ${JSON.stringify(value)}\n\n`);
      };
      send();
      const timer = setInterval(send, 250);
      res.on('close', () => clearInterval(timer));
      return;
    }
    if (details.path === '/slow') {
      res.setHeader('content-type', 'application/json');
      res.writeHead(200);
      res.write('{"ok":true,');
      const timer = setTimeout(() => {
        res.end(JSON.stringify(details).slice(1));
      }, 4000);
      res.on('close', () => clearTimeout(timer));
      return;
    }
    if (details.path === '/' || details.path === '/page') {
      res.setHeader('content-type', 'text/html');
      res.end('<!doctype html><title>proxy measurement</title><script>'
        + script.replace('PAGE_PAYLOAD', JSON.stringify(details)) + '</script>');
      return;
    }
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify(details));
  });
  server.on('upgrade', (req, socket) => {
    const details = describe(req);
    log('websocket', details);
    if (!details.case || !req.headers['sec-websocket-key']) { socket.destroy(); return; }
    const accept = crypto.createHash('sha1')
      .update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '
      + accept + '\r\n\r\n');
    const payload = Buffer.from(JSON.stringify(details));
    const head = payload.length < 126 ? Buffer.from([0x81, payload.length]) : Buffer.from([0x81,126,payload.length>>8,payload.length&255]);
    socket.write(Buffer.concat([head, payload]));
    socket.on('data', data => { if ((data[0] & 15) === 8) socket.end(Buffer.from([0x88,0])); });
    socket.on('error', error => log('websocket_error', {...details, error:String(error)}));
    socket.on('close', () => log('websocket_closed', details));
  });
  server.on('tlsClientError', error => log('tls_client_error', {route, protocol, error:String(error)}));
  server.listen(port, host, () => log('listening', {route, protocol, host, port}));
  return server;
}
const servers = [
  makeOrigin('origin', 'h1', '127.0.0.1', 443),
  makeOrigin('origin', 'h2', '127.0.0.2', 443),
  makeOrigin('proxy-backend', 'h1', '127.0.0.1', 18443),
  makeOrigin('proxy-backend', 'h2', '127.0.0.1', 18444),
];
const proxy = http.createServer((req,res) => { res.writeHead(405); res.end(); });
proxy.on('connect', (req, client, head) => {
  const target = req.url;
  const host = target.endsWith(':443') ? target.slice(0,-4) : '';
  const c = byHost.get(host);
  if (!c) { client.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return; }
  const connection = `connect-${++connectionID}`;
  const mode = proxyModes.get(host) ?? 'transport';
  const upstreamHost = mode === 'origin' && c.origin === 'h2' ? '127.0.0.2' : '127.0.0.1';
  const upstreamPort = mode === 'origin' ? 443 : c.proxy === 'h2' ? 18444 : 18443;
  log('proxy_connect', {case:c.id, host, target, connection, mode, upstreamHost, upstreamPort});
  const upstream = net.connect({host:upstreamHost, port:upstreamPort});
  proxyConnections.set(connection, {host, client, upstream});
  client.on('error', error => log('proxy_client_error', {connection, error:String(error)}));
  upstream.on('error', error => { log('proxy_upstream_error', {connection, error:String(error)}); client.destroy(); });
  client.on('close', () => { upstream.destroy(); proxyConnections.delete(connection); log('proxy_close',{connection, host}); });
  upstream.on('close', () => client.destroy());
  upstream.once('connect', () => {
    const key = `${upstream.localPort}:${upstreamPort}`;
    upstreamConnections.set(key, connection);
    log('proxy_upstream_connected', {case:c.id, connection, sourcePort:upstream.localPort, upstreamHost, upstreamPort});
    upstream.once('close', () => upstreamConnections.delete(key));
    client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    if (head.length) upstream.write(head);
    client.pipe(upstream); upstream.pipe(client);
  });
});
proxy.listen(18080, '127.0.0.1', () => log('proxy_listening', {port:18080}));
function failureListener(kind, port) {
  return net.createServer(client => {
    const connection = `${kind}-${++connectionID}`;
    stalledClients.add(client);
    log(`proxy_${kind}_accept`, {port, connection});
    let header = '';
    client.on('data', chunk => {
      if (header === null) return;
      header += chunk.toString('ascii');
      if (header.length > 8192) { client.destroy(); return; }
      if (!header.includes('\r\n')) return;
      const target = /^CONNECT ([^ ]+) HTTP\/1\.[01]\r\n/.exec(header)?.[1];
      header = null;
      const host = target?.endsWith(':443') ? target.slice(0, -4) : '';
      const c = byHost.get(host);
      if (!c) { client.destroy(); return; }
      log(kind === 'refuse' ? 'proxy_refused' : 'proxy_stall_connect', {port, connection, case:c.id, host, target});
      if (kind === 'refuse') client.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
    });
    client.on('close', () => { stalledClients.delete(client); log('failure_listener_close', {connection}); });
    client.on('error', () => stalledClients.delete(client));
  });
}
const refuseProxy = failureListener('refuse', 18082);
refuseProxy.listen(18082, '127.0.0.1', () => log('proxy_refuse_listening', {port:18082}));
const stallProxy = failureListener('stall', 18083);
stallProxy.listen(18083, '127.0.0.1', () => log('proxy_stall_listening', {port:18083}));
const control = http.createServer(async (req,res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1:18090');
    let result = {ok:true};
    if (url.pathname === '/log') {
      let raw = ''; for await (const chunk of req) raw += chunk;
      result = log('client', JSON.parse(raw));
    } else if (url.pathname === '/seen') {
      result = {ok:true, requests:events.filter(e => (e.event === 'request' || e.event === 'websocket')
        && e.id === url.searchParams.get('id'))};
    } else if (url.pathname === '/close-proxy') {
      const host = url.searchParams.get('host'); const connections = [];
      for (const [id, entry] of proxyConnections) if (entry.host === host) {
        connections.push(id); entry.client.destroy(); entry.upstream.destroy();
      }
      result = log('proxy_forced_close', {ok:true, host, closed:connections.length, connections});
    } else if (url.pathname === '/close-origin') {
      const host = url.searchParams.get('host'); const connections = [];
      for (const socket of originConnections.get(host) ?? []) {
        const opened = events.findLast(e => e.event === 'tls_open' && e.route === 'origin' && e.sni === host && e.peerPort === socket.remotePort);
        if (opened) connections.push(opened.connection);
        socket.destroy();
      }
      result = log('origin_forced_close', {ok:true, host, closed:connections.length, connections});
    } else if (url.pathname === '/absent-check') {
      const host = url.searchParams.get('host'); const c = byHost.get(host);
      if (!c) throw new Error('invalid absent-check host');
      const state = await new Promise(resolve => {
        const socket = net.connect({host:'127.0.0.1', port:18081});
        const done = value => { socket.destroy(); resolve(value); };
        socket.setTimeout(1000, () => done('timeout'));
        socket.on('connect', () => done('listening'));
        socket.on('error', error => done(error.code === 'ECONNREFUSED' ? 'refused' : String(error)));
      });
      result = log('absent_listener_check', {ok:state === 'refused', host, case:c.id, port:18081, state});
    } else if (url.pathname === '/proxy-mode') {
      const host = url.searchParams.get('host'); const mode = url.searchParams.get('mode');
      if (!byHost.has(host) || !['origin','transport'].includes(mode)) throw new Error('invalid mode');
      proxyModes.set(host, mode); log('proxy_mode', {host, mode});
    } else if (url.pathname === '/restart-network') {
      const pid = Number(url.searchParams.get('pid'));
      if (!Number.isInteger(pid) || pid < 2) throw new Error('invalid PID');
      const command = execFileSync('/bin/ps', ['-p', String(pid), '-o', 'command='], {encoding:'utf8', timeout:2000}).trim();
      if (!command.includes('com.apple.WebKit.Networking') ||
          (!command.includes('Simulator') && !command.includes('CoreSimulator'))) {
        throw new Error('refuse non-simulator NetworkProcess: ' + command);
      }
      recordedProcesses.add(pid);
      log('intentional_network_kill', {pid, command});
      process.kill(pid, 'SIGKILL');
      result = {ok:true, pid, command};
    } else if (url.pathname === '/health') {
      const ports = [443,18443,18444,18080,18082,18083,18090];
      result = {ok:servers.every(server => server.listening) && ports.every(port => listening.has(port)),
        cases:cases.length, lifecycleCases:lifecycleCases.length,
        listening:[...listening].sort(), originAddresses:['127.0.0.1:443','127.0.0.2:443']};
    } else { throw new Error('unknown control operation'); }
    res.setHeader('content-type','application/json'); res.end(JSON.stringify(result));
  } catch (error) {
    log('control_error', {error:String(error)});
    res.writeHead(400, {'content-type':'application/json'}); res.end(JSON.stringify({ok:false,error:String(error)}));
  }
});
control.listen(18090, '127.0.0.1', () => log('control_listening', {port:18090}));
process.on('SIGTERM', () => {
  log('fixture_stopping', {recordedProcesses:[...recordedProcesses]});
  for (const entry of proxyConnections.values()) { entry.client.destroy(); entry.upstream.destroy(); }
  for (const client of stalledClients) client.destroy();
  for (const server of [...servers,proxy,refuseProxy,stallProxy,control]) server.close();
  setTimeout(() => process.exit(0), 200).unref();
});
