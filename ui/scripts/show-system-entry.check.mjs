import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { launchSafeBrowser } from './safe-browser.mjs';

// Exercise the server's actual metadata injection and manifest link in a browser.
const server = await readFile(new URL('../../vibe/ui_server.py', import.meta.url), 'utf8');

test('HTML injection preserves document mode, authored nodes and the real head', async (t) => {
  const content = '<main data-author="body"><p>First</p><p>Last</p></main>';
  const title = '<title data-author="title">Authored title</title>';
  const cases = [
    ['explicit head', `<!doctype html><html><head>${title}</head><body>${content}</body></html>`],
    ['quoted head attribute', `<!DOCTYPE html><HTML><HEAD data-note=">">${title}</HEAD><BODY>${content}</BODY></HTML>`],
    ['implicit head with html', `<!doctype html><html><body>${content}</body></html>`],
    ['implicit head without html', `<!doctype html><body>${content}</body>`],
    ['prologue with fake head', ` \n<!-- <head>not a tag</head> -->\n<!doctype html>\n<html><body>${content}</body></html>`],
    ['comment before real head', `<!doctype html><!-- <head>fake</head> --><html><head>${title}</head><body>${content}</body></html>`],
    ['original quirks', `<html><body>${content}</body></html>`],
    ['implicit title', `<!doctype html>${title}<body>${content}</body>`],
    ['leading BOM', `\ufeff<!doctype html><html><body>${content}</body></html>`],
    ['character reference before body', `<!doctype html>&#32;Lead ${content}`],
    ['production sequence', `<!doctype html><html><body>${content}</body></html>`, true],
    ['existing runtime fallback', `<!doctype html>${title}${content}`, true, true],
  ].map(([name, html, runtime = false, existingBoundary = false]) => ({ name, html, runtime, existingBoundary }));
  // Execute the actual insertion functions without importing a live server or
  // touching account/config stores. Only the runtime payload is synthetic.
  const documents = JSON.parse(execFileSync('python3', ['-c', `
from __future__ import annotations
import ast, html, json, re, sys
from html.parser import HTMLParser
from typing import Any
data = json.load(sys.stdin)
names = {
    "_SHOW_SYSTEM_ENTRY_MANIFEST",
    "_SHOW_RUNTIME_MODULE_SCRIPT_RE", "_SHOW_HMR_RESUME_SCRIPT",
    "_inject_show_system_entry", "_inject_show_runtime_config", "_show_runtime_config_script",
}
nodes = [
    node for node in ast.parse(data["source"]).body
    if (isinstance(node, ast.FunctionDef) and node.name in names)
    or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets))
]
scope = dict(html=html, json=json, re=re, Any=Any, HTMLParser=HTMLParser)
scope["_show_runtime_config_payload"] = lambda *args, **kwargs: {"basePath": kwargs["base_path"]}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "actual-show-injectors", "exec"), scope)
inject = lambda value: scope["_inject_show_system_entry"](
    value, title="Synthetic Show", icon_url="/show/test/__show/icon", base_path="/show/test/",
).decode()
for case in data["cases"]:
    baseline = case["html"].encode()
    if case["runtime"]:
        baseline = scope["_inject_show_runtime_config"](
            baseline, "test", base_path="/show/test/",
            authenticated=False, include_write_token=False,
        )
    case["baseline"] = baseline.decode()
    case["injected"] = inject(baseline)
    case["tags"] = inject(b"")
print(json.dumps(data["cases"]))
`], { input: JSON.stringify({ source: server, cases }), encoding: 'utf8' }));
  const browser = await launchSafeBrowser();
  try {
    for (const fixture of documents) {
      await t.test(fixture.name, async () => {
        assert.equal(fixture.injected.replace(fixture.tags, ''), fixture.baseline, 'all authored bytes retain their order');
        const states = {};
        for (const version of ['html', 'baseline', 'injected']) {
          const page = await browser.context.newPage();
          try {
            await page.route('http://show.test/**', (route) => route.request().isNavigationRequest()
              ? route.fulfill({ contentType: 'text/html', body: fixture[version] })
              : route.fulfill({ status: 204, body: '' }));
            await page.goto('http://show.test/show/test/');
            states[version] = await page.evaluate(() => ({
              mode: document.compatMode,
              doctype: document.doctype ? [document.doctype.name, document.doctype.publicId, document.doctype.systemId] : null,
              title: document.title,
              text: document.body.textContent,
              author: [...document.querySelectorAll('[data-author]')].map((node) => [node.dataset.author, node.outerHTML, node.parentElement.tagName]),
              heads: document.querySelectorAll('head').length,
              tags: [...document.querySelectorAll('link, meta')].map((node) => node.parentElement === document.head),
              scripts: document.querySelectorAll('script').length,
              manifest: document.querySelector('head > link[rel="manifest"]')?.getAttribute('href'),
            }));
          } finally {
            await page.close();
          }
        }
        if (fixture.existingBoundary) {
          // Independently expose the older injector's boundary; it is not a
          // waiver for any of the new injector's standards-mode cases above.
          assert.equal(states.html.mode, 'CSS1Compat');
          assert.equal(states.baseline.mode, 'BackCompat');
          assert.equal(states.baseline.doctype, null);
        } else {
          assert.equal(states.baseline.mode, states.html.mode);
          assert.deepEqual(states.baseline.doctype, states.html.doctype);
        }
        assert.equal(states.injected.mode, states.baseline.mode);
        assert.deepEqual(states.injected.doctype, states.baseline.doctype);
        assert.equal(states.injected.title, states.baseline.title);
        assert.equal(states.injected.text, states.baseline.text);
        assert.deepEqual(states.injected.author, states.baseline.author);
        assert.equal(states.injected.heads, 1);
        assert.deepEqual(states.injected.tags, Array(5).fill(true));
        // Native install metadata only: no Avibe-drawn install prompt script.
        assert.equal(states.injected.scripts, states.baseline.scripts);
        assert.equal(states.injected.manifest, '/show/test/__show/manifest.webmanifest');
      });
    }
  } finally {
    await browser.close();
  }
});

test('browser manifest loader carries test identity and rejects missing/revoked grants', async () => {
  // Consume the product-generated link markup, not a parallel link with the desired attribute.
  const template = server.match(/f'(<link rel="manifest"[^']+)'/)?.[1];
  assert.ok(template);
  const requests = [];
  let revoked = false;
  const fixture = createServer((req, res) => {
    const path = new URL(req.url, 'http://fixture').pathname;
    res.setHeader('Cache-Control', 'private, no-store');
    if (path.endsWith('/__show/manifest.webmanifest')) {
      const owner = path.startsWith('/show/') && req.headers.cookie === 'test_identity=owner';
      const recipient = path.startsWith('/p/') && req.headers.cookie === 'test_identity=recipient' && !revoked;
      const allowed = owner || recipient;
      requests.push({ path, cookie: req.headers.cookie, allowed });
      res.writeHead(allowed ? 200 : 403, { 'Content-Type': 'application/manifest+json' });
      const route = path.replace('__show/manifest.webmanifest', '');
      res.end(JSON.stringify(allowed
        ? { id: route, name: 'Test Show', start_url: route, scope: route, display: 'standalone' }
        : { error: 'forbidden' }));
    } else {
      const link = template.replace(/\{[^}]+\}/, `${path}__show/manifest.webmanifest`);
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(`<!doctype html><head>${link}</head><body>Test Show</body>`);
    }
  });
  await new Promise((resolve) => fixture.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${fixture.address().port}`;
  const browser = await launchSafeBrowser();
  try {
    const { context } = browser;
    for (const [identity, route, allowed] of [
      ['owner', '/show/test-owner/', true],
      ['recipient', '/p/test-limited/', true],
      ['', '/p/test-limited/', false],
      ['revoked', '/p/test-limited/', false],
    ]) {
      revoked = identity === 'revoked';
      await context.clearCookies();
      if (identity) await context.addCookies([{
        name: 'test_identity', value: revoked ? 'recipient' : identity, url: origin,
      }]);
      const page = await context.newPage();
      await page.goto(`${origin}${route}`);
      const cdp = await context.newCDPSession(page);
      // Page.getAppManifest invokes Chromium's manifest loader/parser, not page.fetch.
      const manifest = await cdp.send('Page.getAppManifest');
      const read = requests.at(-1);
      assert.equal(read.path, `${route}__show/manifest.webmanifest`);
      assert.equal(read.allowed, allowed, identity);
      assert.equal(new URL(manifest.url).origin, origin);
      if (allowed) {
        assert.equal(read.cookie, `test_identity=${identity}`);
        const parsed = JSON.parse(manifest.data);
        assert.equal(parsed.start_url, route);
        assert.equal(parsed.id, route);
        assert.equal(parsed.scope, route);
        assert.deepEqual(manifest.errors, []);
      } else {
        assert.ok(!manifest.data, 'the browser must not receive protected manifest contents');
      }
      await cdp.detach();
      await page.close();
    }
  } finally {
    await browser.close();
    await new Promise((resolve) => fixture.close(resolve));
  }
});
