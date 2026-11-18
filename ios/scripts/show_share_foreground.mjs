#!/usr/bin/env node
// Serve the real Show sharing design frames and disposable read-only Show APIs.
import { createServer as createHttpServer } from 'node:http';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

const { values } = parseArgs({
  options: {
    'report-file': { type: 'string' },
    help: { type: 'boolean', short: 'h' },
  },
});
if (values.help) {
  console.log(`Usage: node ios/scripts/show_share_foreground.mjs [--report-file <path>]

Print one JSON readiness record with pid, url and backendPort.
Use url as AVIBE_I1_FOREGROUND_ORIGIN for ShowShareForegroundTests.
Both servers bind dynamic loopback ports and accept GET requests only.
SIGINT or SIGTERM closes the servers and removes their temporary cache.`);
  process.exit(0);
}
if (values['report-file'] === '') {
  throw new Error('--report-file requires a nonempty path.');
}

const uiURL = new URL('../../ui/', import.meta.url);
const uiRoot = fileURLToPath(uiURL);
// Resolve the UI's installed dependency, independent of cwd or this script's
// location outside ui/. No global Vite installation or new package is needed.
const uiRequire = createRequire(new URL('package.json', uiURL));
const { createServer: createViteServer } = await import(
  pathToFileURL(uiRequire.resolve('vite')).href
);

const showHTML = `<!doctype html>
<html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;padding:28px;font:17px -apple-system;color:#122025;background:#f6fafc}
h1{font-size:30px}article{margin-top:30px;padding:24px;background:white;border-radius:14px}
strong{font-size:56px;color:#007868}.bar{height:24px;margin:18px 0;border-radius:5px;background:#0084f5}
footer{margin-top:1100px}
</style>
<h1>Q4 漏斗看板</h1><p>Current visible Show content · 当前可见内容</p>
<article><p>Qualified opportunities</p><strong>128</strong>
<p>From first contact to signed agreement</p>
<div class="bar" style="width:100%"></div><div class="bar" style="width:73%"></div>
<div class="bar" style="width:46%"></div><p>Visible snapshot · 可见区域副本</p></article>
<footer>Hidden below the viewport — excluded from this export.</footer></html>`;

const backend = createHttpServer((request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  if (request.method !== 'GET') {
    response.writeHead(405, { Allow: 'GET' });
    response.end();
    return;
  }
  // Match origin-form paths without trusting Host or resolving caller URLs.
  const path = (request.url ?? '').split('?')[0];
  let body;
  if (path === '/api/show-pages/ses_funnel/access') {
    body = { can_use: true, can_manage: false, can_publish_public: false };
  } else if (path === '/api/show-pages/ses_funnel') {
    body = {
      session_id: 'ses_funnel', visibility: 'private', offline: false,
      active_url: '/show/ses_funnel/', title: 'Q4 漏斗看板',
    };
  } else if (path === '/show/ses_funnel/') {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(showHTML);
    return;
  } else {
    response.writeHead(404);
    response.end();
    return;
  }
  response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(body));
});

let vite;
let frontend;
let cacheRoot;
let closing;
async function closeHTTP(server) {
  if (!server?.listening) return;
  await new Promise((resolveClose, rejectClose) => {
    server.close((error) => error ? rejectClose(error) : resolveClose());
    server.closeIdleConnections();
  });
}
async function close() {
  if (!closing) {
    closing = (async () => {
      try {
        if (vite) await vite.close();
      } finally {
        try {
          await Promise.all([closeHTTP(frontend), closeHTTP(backend)]);
        } finally {
          if (cacheRoot) await rm(cacheRoot, { recursive: true, force: true });
        }
      }
    })();
  }
  return closing;
}

try {
  await new Promise((resolveListen, rejectListen) => {
    backend.once('error', rejectListen);
    backend.listen(0, '127.0.0.1', () => {
      backend.removeListener('error', rejectListen);
      resolveListen();
    });
  });
  const backendPort = backend.address().port;
  const backendOrigin = `http://127.0.0.1:${backendPort}`;
  // The repository's Vite config proxies APIs to this process-owned server.
  // Override inherited targets and do not load a local account's .env files.
  process.env.VIBE_UI_BACKEND = backendOrigin;
  cacheRoot = await mkdtemp(join(tmpdir(), 'avibe-show-share-'));
  vite = await createViteServer({
    root: uiRoot,
    configFile: fileURLToPath(new URL('vite.config.ts', uiURL)),
    envFile: false,
    cacheDir: join(cacheRoot, 'vite'),
    logLevel: 'silent',
    clearScreen: false,
    server: {
      // Own the HTTP listener and signals: Vite's standalone server installs
      // a SIGTERM handler that can exit before the fixture cache is removed.
      middlewareMode: true, hmr: false, open: false,
      cors: false,
      // /show is an actual document, not Vite's SPA fallback.
      proxy: { '/show': { target: backendOrigin, changeOrigin: true } },
    },
  });
  frontend = createHttpServer((request, response) => {
    if (request.method !== 'GET') {
      response.writeHead(405, { Allow: 'GET' });
      response.end();
      return;
    }
    vite.middlewares(request, response, () => {
      response.writeHead(404);
      response.end();
    });
  });
  await new Promise((resolveListen, rejectListen) => {
    frontend.once('error', rejectListen);
    frontend.listen(0, '127.0.0.1', () => {
      frontend.removeListener('error', rejectListen);
      resolveListen();
    });
  });
  const report = {
    pid: process.pid,
    url: `http://127.0.0.1:${frontend.address().port}`,
    backendPort,
  };
  if (values['report-file']) {
    await writeFile(resolve(values['report-file']), `${JSON.stringify(report)}\n`, {
      flag: 'wx', mode: 0o600,
    });
  }
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, () => {
      close().then(
        () => process.exit(0),
        (error) => { console.error(error); process.exit(1); },
      );
    });
  }
  console.log(JSON.stringify(report));
} catch (error) {
  await close();
  throw error;
}
