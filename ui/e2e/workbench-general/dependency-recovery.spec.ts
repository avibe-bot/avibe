import { expect, test } from '../support/browserTest';
import { open, serveProduct } from './support';
import en from '../../src/i18n/en.json' with { type: 'json' };

const copy = en.settings.dependencies;
const ready = (ids: string[]) => ({
  ok: true,
  deps: ids.map((id) => ({
    id, kind: 'runtime', required: true, installed: true, status: 'ready', version: '1.2.3',
  })),
});

test('production dependency deadlines are shown as timeouts and recover after one delayed read', async ({ page }, info) => {
  const denied = await serveProduct(page);
  let available = false;
  let reads = 0;
  await page.route('**/api/dependencies?**', async (route) => {
    reads++;
    // Leaving the first request unanswered exercises the browser's real fetch
    // cancellation, including WebKit's fresh AbortError object.
    if (!available) return;
    await route.fulfill({ json: ready(new URL(route.request().url()).searchParams.getAll('id')) });
  });
  await page.clock.install();
  await open(page, '/settings/dependencies');
  await expect.poll(() => reads).toBe(5);
  await page.clock.runFor(15_001);
  await expect(page.getByText(copy.checkTimeout, { exact: true })).toHaveCount(6);
  await expect(page.getByText(copy.checkFailed, { exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('timeout.png') });
  available = true;
  await page.clock.runFor(1_500);
  await expect.poll(() => reads).toBe(10);
  await expect(page.getByText(copy.checkTimeout, { exact: true })).toHaveCount(0);
  await expect(page.getByText(/1\.2\.3/)).toHaveCount(6);
  await page.clock.runFor(120_000);
  expect(reads).toBe(10);
  await page.screenshot({ path: info.outputPath('recovered.png') });
  expect(denied.filter((request) => !request.includes('fonts.'))).toEqual([]);
});

for (const signal of ['online', 'reactivated'] as const) {
  test(`production failed checks stop polling and recover when ${signal}`, async ({ page }) => {
    await serveProduct(page);
    await page.addInitScript(() => {
      let focused = true;
      Object.defineProperty(document, 'hasFocus', { value: () => focused });
      window.addEventListener('blur', () => { focused = false; });
      window.addEventListener('focus', () => { focused = true; });
    });
    let available = false;
    let reads = 0;
    await page.route('**/api/dependencies?**', (route) => {
      reads++;
      return route.fulfill(available
        ? { json: ready(new URL(route.request().url()).searchParams.getAll('id')) }
        : { status: 503, json: { ok: false } });
    });
    await page.clock.install();
    await open(page, '/settings/dependencies');
    await expect(page.getByText(copy.checkFailed, { exact: true })).toHaveCount(6);
    await page.clock.runFor(1_500);
    await expect.poll(() => reads).toBe(10);
    await expect(page.getByText(copy.checkFailed, { exact: true })).toHaveCount(6);
    await page.clock.runFor(120_000);
    expect(reads).toBe(10);
    available = true;
    await page.evaluate((kind) => {
      if (kind === 'online') window.dispatchEvent(new Event('online'));
      else {
        window.dispatchEvent(new Event('blur'));
        window.dispatchEvent(new Event('focus'));
      }
    }, signal);
    await expect.poll(() => reads).toBe(15);
    await expect(page.getByText(copy.checkFailed, { exact: true })).toHaveCount(0);
    await expect(page.getByText(/1\.2\.3/)).toHaveCount(6);
    await page.clock.runFor(120_000);
    expect(reads).toBe(15);
  });
}
