import { writeFile } from 'node:fs/promises';
import { expect, test } from '../support/browserTest';
import { clientState, installBridge, signIn } from './bridge-fixture';
import config from '../../../desktop/src-tauri/tauri.conf.json' with { type: 'json' };
// Browser reference geometry from native configuration, not an OS capture.
const controls = config.app.windows.find(window => window.label === 'main')!.trafficLightPosition!;
test('Account back button edge follows configured logical control inset without moving content or vertical position', async ({ page }, info) => {
  let projected = true;
  await page.setViewportSize({ width: 880, height: 600 });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:5214') return route.abort();
    if (url.pathname === '/account/index.html') {
      const response = await route.fetch();
      return route.fulfill({ response, body: (await response.text()).replace('<head>', `<head><style>:root{--shell-titlebar-inset:28px;${projected ? `--shell-window-controls-left:${controls.x}px` : ''}}</style>`) });
    }
    return route.continue();
  });
  const state = clientState(); signIn(state); await installBridge(page, state);
  await page.addInitScript(() => localStorage.setItem('i18nextLng', 'zh'));
  await page.goto('http://127.0.0.1:5214/account/index.html?view=instances');
  await expect(page.getByRole('button', { name: '返回工作台' })).toBeVisible();
  const measure = () => page.evaluate(() => {
    const button = document.querySelector('header button')!;
    const rect = button.querySelector('svg')!.getBoundingClientRect();
    const box = button.getBoundingClientRect();
    return { x: rect.x, y: rect.y, edge: box.x, inset: parseFloat(getComputedStyle(button).paddingLeft), height: box.height, mainPadding: getComputedStyle(document.querySelector('main')!).paddingLeft };
  });
  const actual = await measure();
  await page.evaluate(({ x, y }) => {
    const reference = document.createElement('div'); reference.dataset.simulatedControls = 'true';
    reference.style.cssText = `position:fixed;left:${x}px;top:${y}px;display:flex;gap:8px;pointer-events:none;z-index:9999`;
    for (const color of ['#ff5f57', '#febc2e', '#28c840']) {
      const dot = document.createElement('span'); dot.style.cssText = `width:12px;height:12px;border-radius:50%;background:${color}`; reference.append(dot);
    }
    document.body.append(reference);
  }, controls);
  await page.screenshot({ animations: 'disabled', path: info.outputPath('back-inset-reference-zh.png') });
  // Under the pointer the wash starts at the controls' origin and the arrow keeps the button's own inset inside it.
  await page.getByRole('button', { name: '返回工作台' }).hover();
  await page.screenshot({ animations: 'disabled', path: info.outputPath('back-inset-hover-zh.png'), clip: { x: 0, y: 0, width: 320, height: 90 } });
  await info.attach('geometry', { body: JSON.stringify({ simulatedControlOrigin: controls, actual }), contentType: 'application/json' });
  projected = false;
  await page.reload(); await expect(page.getByRole('button', { name: '返回工作台' })).toBeVisible();
  const fallback = await measure();
  await writeFile(info.outputPath('geometry.json'), JSON.stringify({ actual, fallback, controls }, null, 2));
  expect(actual.edge).toBe(controls.x); expect(actual.inset).toBeGreaterThan(0); expect(actual.x).toBe(controls.x + actual.inset);
  expect(fallback.inset).toBe(actual.inset); expect(fallback.x).toBe(48);
  expect(actual.y).toBe(fallback.y); expect(actual.height).toBe(fallback.height); expect(actual.mainPadding).toBe(fallback.mainPadding);
});
