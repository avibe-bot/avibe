import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

// AUTH-SETUP-183: run after the Desktop build and the Rust fixture build.
// All network traffic is restricted to this probe's loopback servers.
const source = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../..');
const out = path.resolve(process.argv[2] ?? '/tmp/avibe-provider-callback-evidence');
await fs.mkdir(out, { recursive: true });
const dist = path.join(source, 'desktop/dist');
const binary = path.join(source, 'desktop/target/debug/examples/provider_callback_fixture');
const { chromium } = await import(pathToFileURL(path.join(source, 'ui/node_modules/@playwright/test/index.mjs')));
let locale = 'zh';
const results = [];
const children = [];
function native(mode) {
  const child = spawn(binary, [mode, locale], { stdio: ['pipe', 'pipe', 'pipe'] });
  children.push(child);
  let readyResolve, doneResolve, doneReject;
  const ready = new Promise(r => { readyResolve = r; });
  const done = new Promise((r, j) => { doneResolve = r; doneReject = j; });
  const lines = [];
  createInterface({ input: child.stdout }).on('line', line => {
    const event = JSON.parse(line); lines.push(event);
    if (event.event === 'ready') readyResolve(event);
    else if (event.event === 'result') doneResolve(event);
  });
  child.on('error', doneReject);
  return { child, ready, done, lines };
}
const server = http.createServer(async (req, res) => {
  try {
    const file = path.join(dist, new URL(req.url, 'http://fixture').pathname);
    if (!file.startsWith(dist + '/')) throw Error('path');
    const bytes = await fs.readFile(file);
    res.setHeader('Content-Type', ({'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png'})[path.extname(file)] ?? 'application/octet-stream');
    res.end(bytes);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true });
try {
  for (locale of ['zh', 'en']) {
    const catalog = JSON.parse(await fs.readFile(path.join(source, `ui/src/i18n/${locale}.json`), 'utf8'));
    for (const scenario of ['github-error', 'google-error', 'apple-error', 'fragment-error', 'server-error', 'denied', 'success', 'fragment-success', 'cancel', 'timeout', 'abandon']) {
      const context = await browser.newContext({ viewport: {width: 1100, height: 850}, locale:locale === 'zh' ? 'zh-CN' : 'en-US', serviceWorkers:'block' });
      const page = await context.newPage();
      const allowed = new Set([origin]);
      const denied = [], calls = [], errors = [];
      await context.route('**/*', route => {
        const u = new URL(route.request().url());
        if (allowed.has(u.origin) && route.request().method() === 'GET') return route.continue();
        denied.push(route.request().method() + ' ' + u.origin + u.pathname);
        return route.abort();
      });
      page.on('pageerror', error => errors.push(error.message));
      const state = {version:1,revision:1,generation:1,account:{status:'signed_out',identity:null,email:null,gate:null,methods:[]},connection:{status:'local',committed:{kind:'local',name:'Diagnostic fixture',instance_id:null},pending:null},enrollment:{status:'unpaired',instance_id:null,error_code:null},catalog:{status:'idle',instances:[]},local:{name:'Diagnostic fixture',remote_access:'off'},desktop:{version:'fixture',update:{phase:'idle',latest:null,channel:'test',skippedVersion:null,interactive:false}},instance_update:null,consent:null,remote_auxiliary:'local',sign_in:{providers:['github','google','apple'],passkey:false}};
      let attempt, nativeResult;
      await page.exposeFunction('__fixtureRequest', async request => {
        calls.push(request.action.type);
        let code;
        if (request.action.type === 'sign_in_with_provider') {
          state.generation++;
          attempt = native(['timeout', 'abandon'].includes(scenario) ? scenario : 'normal');
          nativeResult = await attempt.done;
          if (nativeResult.code === 'success') {
            state.account = {status:'signed_in',identity:{id:'fixture',email:'fixture@example.test'},email:null,gate:null,methods:[]};
          } else code = nativeResult.code;
        } else if (request.action.type === 'abort_sign_in') {
          state.generation++;
          attempt.child.stdin.write('cancel\n');
        }
        state.revision++;
        return {version:1,request_id:request.request_id,ok:!code,state:structuredClone(state),...(code ? {error:{code}} : {})};
      });
      await page.addInitScript(initial => {
        Object.defineProperty(window,'__AVIBE_DESKTOP_SHELL__',{value:true});
        Object.defineProperty(window,'__AVIBE_DESKTOP_CLIENT_VERSION__',{value:1});
        Object.defineProperty(window,'__AVIBE_DESKTOP_CLIENT_STATE__',{value:initial});
        Object.defineProperty(window,'__TAURI_INTERNALS__',{value:{invoke:(cmd,args)=>{
          if(cmd!=='desktop_client_request') throw Error('Blocked IPC '+cmd);
          return window.__fixtureRequest(args.request);
        }}});
        if(navigator.credentials) navigator.credentials.get=()=>{throw Error('Credential API forbidden');};
      },state);
      await page.goto(origin+`/account/index.html?locale=${locale}`, {waitUntil:'networkidle'});
      const provider = scenario.split('-')[0] === 'google' ? 'Google' : scenario.split('-')[0] === 'apple' ? 'Apple' : 'GitHub';
      await page.getByRole('button',{name:new RegExp(provider)}).click();
      await page.getByText(catalog.signIn.provider.continueInBrowser,{exact:true}).waitFor();
      assert(attempt, 'Native fixture was started by the production UI');
      const ready = await attempt.ready;
      allowed.add(new URL(ready.redirect).origin);
      let callbackPage, responseStatus, responseBody;
      const callback = new URL(ready.redirect);
      if(scenario.includes('success')) callback.searchParams.set('code','synthetic-code');
      else {
        callback.searchParams.set('error',scenario === 'denied' ? 'access_denied' : 'server_error');
        if(!['denied','server-error'].includes(scenario)) {
          callback.searchParams.set('error_code','unexpected_failure');
          callback.searchParams.set('error_description','Multiple accounts with the same email address in the same linking domain detected: default');
        } else callback.searchParams.set('error_description','<script>window.__unsafe = true</script>');
      }
      if(scenario.startsWith('fragment-')) {
        const fragment = new URLSearchParams(callback.search);
        fragment.delete('state');
        callback.search = new URL(ready.redirect).search;
        callback.hash = fragment.toString();
      } else callback.hash = 'error=server_error&error_description=duplicate-fragment';
      if(scenario === 'cancel') await page.getByRole('button',{name:catalog.common.cancel,exact:true}).click();
      if(['cancel','timeout','abandon'].includes(scenario)) await attempt.done;
      callbackPage = await context.newPage();
      const response = await callbackPage.goto(callback.href);
      responseStatus = response.status();
      assert.equal(responseStatus,200);
      const terminalKey = scenario.includes('success') ? 'done'
        : ['cancel','abandon'].includes(scenario) ? 'cancelled'
        : scenario === 'timeout' ? 'timedOut'
        : scenario === 'denied' ? 'denied'
        : scenario === 'server-error' ? 'serverError' : 'accountConflict';
      await callbackPage.getByRole('heading', {name:catalog.desktopBootstrap.providerCallback[terminalKey],exact:true}).waitFor();
      responseBody = await callbackPage.locator('body').innerText();
      assert.equal(await callbackPage.evaluate(() => window.__unsafe), undefined);
      assert.equal(await callbackPage.getByRole('link').getAttribute('href'), 'avibe://open');
      await callbackPage.screenshot({path:path.join(out,`${locale}-${scenario}-browser.png`)});
      for(const target of [callback.href, new URL('/prefetch',callback).href]) {
        const repeated = await context.newPage();
        assert.equal((await repeated.goto(target)).status(),200);
        assert.equal(await repeated.locator('body').innerText(),responseBody);
        await repeated.close();
      }
      const favicon = await context.request.get(new URL('/favicon.ico', callback).href);
      assert.equal(favicon.status(),200);
      assert((await favicon.text()).includes(catalog.desktopBootstrap.providerCallback[terminalKey]));
      assert.equal((await callbackPage.reload()).status(),200);
      assert.equal(await callbackPage.locator('body').innerText(),responseBody);
      await page.getByText(catalog.signIn.provider.continueInBrowser,{exact:true}).waitFor({state:'hidden'});
      const alerts = await page.getByRole('alert').allTextContents();
      if(scenario.endsWith('-error') || ['timeout','denied'].includes(scenario)) {
        const key = scenario === 'timeout' ? 'timedOut' : scenario === 'denied' ? 'denied'
          : scenario === 'server-error' ? 'serverError' : 'accountConflict';
        assert(alerts.some(text=>text.includes(catalog.desktopBootstrap.providerError[key])));
        assert(await page.getByRole('button',{name:new RegExp(provider)}).isEnabled());
        assert(await page.getByRole('button',{name:catalog.mobileApp.welcome.continueWithEmail,exact:true}).isEnabled());
      }
      if(['cancel','abandon'].includes(scenario)) assert.equal(alerts.length,0);
      if(scenario.includes('success')) assert.equal(state.account.status,'signed_in');
      await page.screenshot({path:path.join(out,`${locale}-${scenario}-desktop.png`)});
      assert.equal(denied.length,0);
      assert.equal(errors.length,0);
      results.push({locale,scenario,nativeResult,alerts,calls,responseStatus,responseBody,denied,errors});
      console.log(JSON.stringify(results.at(-1)));
      await context.close();
      if(attempt.child.exitCode === null) attempt.child.kill();
    }
  }
} finally {
  for(const child of children) if(child.exitCode === null) child.kill();
  await browser.close();
  await new Promise(r=>server.close(r));
  await fs.writeFile(path.join(out,'provider-result-page-results.json'),JSON.stringify(results,null,2));
}
