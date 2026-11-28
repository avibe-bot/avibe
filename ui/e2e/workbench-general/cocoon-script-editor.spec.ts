import { expect, test } from '../support/browserTest';
import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import { DESKTOP, NARROW, open, serveProduct } from './support';

// The actual Settings → launcher → Editor boundary: mocks cannot prove the Settings overlay
// yields foreground, the mobile route receives params, or the explorer reads the hook directory.
const DIR = '/fixture/中文 hooks/source_filter';
const NAMES = ['before_search', 'after_search', 'before_fetch', 'after_fetch', 'after_read', 'after_bash'];
for (const { lang, viewport, missing } of [
  { lang: 'en', viewport: DESKTOP, missing: false },
  { lang: 'zh', viewport: NARROW, missing: false },
  { lang: 'en', viewport: DESKTOP, missing: true },
] as const) {
  test(`cocoon ${lang} ${viewport.width}: opens ${missing ? 'missing' : 'existing'} script in the Editor and roots its explorer`, async ({ page, context }, info) => {
    const isolation = '../../scripts/safe-browser.mjs';
    const { installBrowserIsolation, assertSafePage } = await import(isolation);
    await installBrowserIsolation(context);
    const denied = await serveProduct(page, lang);
    await page.setViewportSize(viewport);
    const reads: string[] = [];
    const file = missing ? 'before_fetch.py' : 'after_read.py';
    const path = `${DIR}/${file}`;
    const notFound = { ok: false, error: { code: 'not_found', message: 'Fixture missing file' } };
    await page.route('**/api/settings/experimental/source-filter', route => route.fulfill({ json: {
      ok: true, enabled: true, dir: DIR,
      hooks: Object.fromEntries(NAMES.map(name => [name, { found: name === 'after_read', last_failure: null }])),
    } }));
    await page.route('**/api/files/**', async route => {
      const request = route.request();
      expect(request.method()).toBe('GET'); // Opening must not write/create/run a hook.
      const url = new URL(request.url());
      const requestedPath = url.searchParams.get('path');
      reads.push(`${url.pathname}:${requestedPath}`);
      if (url.pathname === '/api/files/list') {
        expect(requestedPath).toBe(DIR);
        return route.fulfill({ json: { ok: true, path: DIR, parent: '/fixture/中文 hooks', entries: [
          { name: 'after_read.py', kind: 'file', ext: 'py', size: 40, mtime: 42 },
        ] } });
      }
      if (url.pathname === '/api/files/meta') {
        expect(requestedPath).toBe(path);
        return route.fulfill({ status: missing ? 404 : 200, json: missing ? notFound : {
          ok: true, name: file, ext: 'py', kind: 'file', size: 40, mtime: 42, mime: 'text/plain', text: true,
        } });
      }
      if (url.pathname === '/api/files/content') {
        expect(requestedPath).toBe(path);
        return missing ? route.fulfill({ status: 404, json: notFound }) : route.fulfill({
          contentType: 'text/plain', body: '# Hook script fixture\nprint("fixture")\n',
        });
      }
      return route.fulfill({ json: { ok: true, favorites: [] } });
    });
    await open(page, '/settings/experimental/source-filter', { lang });
    for (const name of NAMES) await expect(page.getByRole('button', { name: `${name}.py`, exact: true })).toBeEnabled();
    await page.getByRole('button', { name: file, exact: true }).click();
    const copy = lang === 'en' ? en : zh;
    if (viewport.width === DESKTOP.width) {
      await expect(page.getByRole('dialog', { name: /Editor/ })).toBeVisible();
      await expect(page.locator('[data-settings-overlay="true"]')).toHaveCount(0);
    } else {
      await expect(page).toHaveURL(/\/apps\/editor$/);
    }
    if (missing) {
      await expect(page.getByText(copy.apps.fileBrowser.errors.not_found, { exact: true })).toBeVisible();
      await expect(page.locator('.monaco-editor')).toHaveCount(0);
    } else {
      await expect(page.locator('.monaco-editor .view-lines').first()).toContainText('Hook script fixture');
    }
    // The mobile explorer is hidden until explicitly opened, but it retains the supplied root.
    if (viewport.width === NARROW.width) await page.getByRole('button', { name: copy.apps.editor.explorer, exact: true }).click();
    await expect.poll(() => reads).toContain(`/api/files/list:${DIR}`);
    expect(reads).toContain(`/api/files/meta:${path}`);
    expect(reads).toContain(`/api/files/content:${path}`);
    expect(denied).toEqual([]);
    // Both clipboard paths are isolated. The synthetic fixture contains no mnemonic, passphrase,
    // quiz or suggestion content; mask password fields before saving any image.
    await assertSafePage(page);
    await page.screenshot({ path: info.outputPath('editor.png'), animations: 'disabled', mask: [page.locator('input[type="password"]')] });
  });
}
