// The rc gate's stand-in instance: the built Web UI of a release candidate
// and just enough of the instance's API for that page to start, be signed in
// or not, and return from the background. It serves the real page; every
// answer here is a stub, and says how its request arrived as the instance
// does (`X-Avibe-Via`, the stream's `via`): `direct` through the proxy's port
// (the stand-in for the App's listener), `other` on WebKit's own connection.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.webmanifest': 'application/manifest+json', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.ico': 'image/x-icon', '.map': 'application/json', '.wasm': 'application/wasm',
};
const CAPABILITIES = Object.fromEntries([
  'is_instance_owner', 'can_read_instance', 'can_chat', 'can_manage_projects', 'can_manage_agents',
  'can_manage_instance', 'can_manage_access_members', 'can_use_model_hub_reset_cards', 'can_use_agents',
  'can_use_skills', 'can_use_vault_secrets', 'can_use_show_pages', 'can_use_terminal_files',
  'can_use_terminal', 'can_use_files', 'can_use_system',
].map(name => [name, true]));

export function makeGate(distDir, log) {
  // host -> 'current' (signed in), 'missing' (no session: what the instance
  // answers for a missing or expired one), 'redirect' (the session read is
  // redirected to another origin, which the instance never does).
  const sessions = new Map();
  const stateOf = host => sessions.get(host) ?? 'current';
  const root = distDir ? path.resolve(distDir) : null;

  function file(res, relative) {
    if (!root) return false;
    const resolved = path.resolve(root, '.' + path.posix.normalize('/' + relative));
    if (resolved !== root && !resolved.startsWith(root + path.sep)) return false;
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) return false;
    const index = path.basename(resolved) === 'index.html';
    res.writeHead(200, {
      'content-type': TYPES[path.extname(resolved)] ?? 'application/octet-stream',
      'cache-control': index ? 'no-store, private'
        : relative.startsWith('assets/') ? 'public, max-age=31536000, immutable' : 'public, max-age=3600',
    });
    res.end(fs.readFileSync(resolved));
    return true;
  }

  return {
    ready: () => Boolean(root && fs.existsSync(path.join(root, 'index.html'))),
    set(host, state) {
      if (!['current', 'missing', 'redirect'].includes(state)) throw new Error('invalid session state');
      sessions.set(host, state);
      return log('gate_session', {ok: true, host, mode: state});
    },
    request(req, res, details) {
      const via = details.proxyConnection ? 'direct' : 'other';
      const p = details.path;
      log('gate_request', {
        ...details, via, page: req.headers['x-avibe-page'] ?? null,
        fetchMode: req.headers['sec-fetch-mode'] ?? null, fetchDest: req.headers['sec-fetch-dest'] ?? null,
      });
      const json = (status, body) => {
        res.writeHead(status, {'content-type': 'application/json', 'cache-control': 'private, no-store', 'x-avibe-via': via});
        res.end(JSON.stringify(body));
      };
      const state = stateOf(details.host);
      if (p === '/auth/login') {
        res.writeHead(200, {'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store'});
        res.end('<!doctype html><title>gate sign-in</title><p id="gate-sign-in">sign-in</p>');
        return;
      }
      if (p === '/api/session') {
        if (state === 'redirect') {
          res.writeHead(302, {location: 'https://elsewhere.webkit-proxy.test/landing', 'cache-control': 'no-store'});
          res.end();
        } else if (state === 'missing') {
          // vibe/ui_server.py api_session: no identity, or one that is not current.
          json(200, {remote: true, authenticated: false});
        } else {
          json(200, {
            remote: true, authenticated: true, email: 'gate@example.test', sub: 'gate', author_id: 'remote:gate',
            instance_kind: 'personal', instance_role: 'owner', capabilities: CAPABILITIES,
            event_stream_transport: 'websocket-sse-v1', authorization_state: 'current', instance_id: 'gate',
            authorization_policy: 'personal', session_expires_at: Math.floor(Date.now() / 1000) + 86400,
            access_digest: 'gate',
          });
        }
        return;
      }
      if (p.startsWith('/api/')) {
        if (state !== 'current') { json(401, {ok: false, error: 'remote_access_login_required'}); return; }
        if (p === '/api/csrf-token') { json(200, {csrf_token: 'gate'}); return; }
        if (p === '/api/version') { json(200, {current: '3.2.9rc6', latest: '3.2.9rc6', has_update: false}); return; }
        if (req.method !== 'GET') { json(200, {ok: true}); return; }
        json(200, {});
        return;
      }
      if (p === '/status') { json(200, {}); return; }
      const relative = p.replace(/^\/+/, '');
      if (relative && file(res, relative)) return;
      if (relative.startsWith('assets/') || /\.[a-z0-9]+$/i.test(relative)) { res.writeHead(404); res.end(); return; }
      // Any other path is a route of the page.
      if (!file(res, 'index.html')) { res.writeHead(503); res.end('no built page'); }
    },
    // The event stream: a WebSocket that carries the server's event text.
    upgrade(req, socket, details) {
      if (details.path !== '/api/events' || !req.headers['sec-websocket-key']) return false;
      const via = details.proxyConnection ? 'direct' : 'other';
      log('gate_stream', {...details, via});
      if (stateOf(details.host) !== 'current') { socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return true; }
      const accept = crypto.createHash('sha1')
        .update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
      socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '
        + accept + '\r\n\r\n');
      const send = text => {
        const payload = Buffer.from(text);
        const head = payload.length < 126 ? Buffer.from([0x81, payload.length])
          : Buffer.from([0x81, 126, payload.length >> 8, payload.length & 255]);
        if (!socket.destroyed) socket.write(Buffer.concat([head, payload]));
      };
      const interval = 5000;
      send(`event: connected\ndata: ${JSON.stringify({sub_id: 1, interval_ms: interval, via})}\n\n`);
      const beat = setInterval(() => send(`event: heartbeat\ndata: {"interval_ms":${interval}}\n\n`), interval);
      socket.on('data', data => { if ((data[0] & 15) === 8) socket.end(Buffer.from([0x88, 0])); });
      socket.on('error', () => undefined);
      socket.on('close', () => { clearInterval(beat); log('gate_stream_closed', details); });
      return true;
    },
  };
}
