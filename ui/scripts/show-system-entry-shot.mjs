// Render the exact server-injected UI, not the design kit, without a live Runtime.
// All requests are fulfilled locally; this is visual evidence, not Safari acceptance.
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { launchSafeBrowser } from './safe-browser.mjs';

const out = resolve(process.argv[2] || 'design/.shots/system-entry-runtime-actual');
await mkdir(out, { recursive: true });
const server = await readFile(new URL('../../vibe/ui_server.py', import.meta.url), 'utf8');
const script = server.match(/_SHOW_SYSTEM_ENTRY_SCRIPT_BODY = r"""([\s\S]*?)"""/)?.[1];
assert.ok(script);
const evidence = [];
for (const lang of ['en', 'zh']) {
  const copy = JSON.parse(await readFile(new URL(`../../vibe/i18n/${lang}.json`, import.meta.url), 'utf8')).show.systemEntry;
  for (const [platform, width, height, ua] of [
    ['iphone', 390, 844, 'Mozilla/5.0 (iPhone) Version/18.0 Mobile Safari/605.1.15'],
    ['mac', 1280, 760, 'Mozilla/5.0 (Macintosh) Version/18.0 Safari/605.1.15'],
  ]) {
    for (const theme of ['light', 'dark']) {
      const browser = await launchSafeBrowser({ contextOptions: {
        viewport: { width, height }, colorScheme: theme, deviceScaleFactor: 2,
      } });
      try {
        const { context } = browser;
        await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html>
<html lang="${lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{color-scheme:light dark}body{margin:0;background:light-dark(#f6f8fa,#11191e);color:light-dark(#17202a,#eef4f6);font:15px/1.6 system-ui}
main{box-sizing:border-box;min-height:calc(100vh - 140px);max-width:900px;margin:auto;padding:24px;display:flex;flex-direction:column}
h1{font-size:28px;line-height:1.2}article{border:1px solid #74818b55;border-radius:16px;padding:20px;background:light-dark(white,#1a2227)}
small{opacity:.7}#last-line{margin-top:auto;padding-top:20px;font-size:13px}button[data-show-annotation-ui]{position:fixed;right:26px;bottom:26px;width:52px;height:52px;border:1px solid #74818b66;border-radius:16px;background:light-dark(white,#1a2227);color:inherit;z-index:2147483200}
</style></head><body><main><small>Show Page · ${lang === 'zh' ? '受限分享测试内容' : 'Limited-share test content'}</small>
<h1>${lang === 'zh' ? '健康接入流程' : 'Health connection flow'}</h1><article><strong>${lang === 'zh' ? '准备连接设备' : 'Connect your device'}</strong><p>${lang === 'zh' ? '页面内容保持可见。系统入口提供添加步骤，访问权限仍由分享者管理。' : 'The authored content stays visible. System entry guidance explains installation; access remains controlled by the owner.'}</p><small>/p/safari-acceptance/ · browser</small></article>
<p id="last-line">${lang === 'zh' ? '页面末行 — 展开指引后仍完整可见。' : 'END OF TEST CONTENT — this line must remain reachable.'}</p></main><button data-show-annotation-ui aria-label="Annotate">✎</button></body></html>` }));
        await context.addInitScript((agent) => Object.defineProperty(navigator, 'userAgent', { value: agent }), ua);
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', (error) => errors.push(String(error)));
        await page.goto('https://show.test/p/safari-acceptance/');
        await page.evaluate((config) => {
          const meta = document.createElement('meta');
          meta.name = 'avibe-show-system-entry';
          meta.content = JSON.stringify(config);
          document.head.append(meta);
        }, { copy });
        await page.addScriptTag({ content: script });
        const entry = page.getByRole('button', { name: platform === 'iphone' ? copy.iosCta : copy.macCta, exact: true });
        for (const state of ['collapsed', 'expanded']) {
          if (state === 'expanded') {
            await entry.click();
            await page.waitForFunction(() => document.querySelector('[data-avibe-show-system-entry]')?.shadowRoot.activeElement?.classList.contains('collapse'));
          }
          const file = `${platform}-${lang}-${theme}-${state}.png`;
          const geometry = await page.evaluate(() => {
            const rect = (element) => { const r = element.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
            const panel = document.querySelector('[data-avibe-show-system-entry]').shadowRoot.querySelector('.panel');
            return { lastLine: rect(document.querySelector('#last-line')), panel: panel.hidden ? null : rect(panel), annotation: rect(document.querySelector('[data-show-annotation-ui]')), path: location.pathname };
          });
          if (state === 'expanded') {
            assert.ok(geometry.lastLine.bottom <= geometry.panel.top);
            assert.ok(geometry.lastLine.top >= 0);
            assert.ok(geometry.panel.bottom <= geometry.annotation.top);
          }
          assert.deepEqual(errors, []);
          await page.screenshot({ path: resolve(out, file) });
          evidence.push({ file, platform, lang, theme, state, geometry, errors });
        }
      } finally { await browser.close(); }
    }
  }
}
await writeFile(resolve(out, 'evidence.json'), JSON.stringify(evidence, null, 2));
console.log(JSON.stringify({ out, screenshots: evidence.length, source: 'vibe/ui_server.py exact injected script', realSafari: false }));
