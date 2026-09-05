import { expect, test, type Page } from '@playwright/test';
import { copy } from '../support/copy';

const fixture = (page: Page) => page.getByTestId('chat-paging-fixture');
const scroller = (page: Page) => page.getByTestId('chat-transcript');
const loads = async (page: Page) => Number(await fixture(page).getAttribute('data-loads'));
const open = async (page: Page, query = '') => {
  // No API request from this fixture may reach a running Avibe service.
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto(`/e2e/chat-paging/fixture.html${query}`);
  await expect.poll(() => scroller(page).evaluate((el) => el.scrollTop)).toBeGreaterThan(120);
};

test('each upward visit loads one page and preserves the reader through the retained-window cap', async ({ page }, testInfo) => {
  await open(page);
  for (let index = 0; index < 8; index += 1) {
    const anchorId = await page.locator('[data-message-id]').first().getAttribute('data-message-id');
    const anchor = page.locator(`[data-message-id="${anchorId}"]`);
    await scroller(page).evaluate((el) => { el.scrollTop = 0; });
    const top = await anchor.evaluate((el) => el.getBoundingClientRect().top);
    await expect(fixture(page)).toHaveAttribute('data-loads', String(index + 1));
    await expect(page.getByRole('status', { name: copy('chat.loadingOlder') })).toHaveCount(0);
    await expect.poll(() => scroller(page).evaluate((el) => el.scrollTop)).toBeGreaterThan(120);
    expect(Math.abs(await anchor.evaluate((el) => el.getBoundingClientRect().top) - top)).toBeLessThan(1);
    await expect(page.locator('[data-message-id]')).toHaveCount(Math.min(100 + index * 50, 300));
    // Observe an idle interval: finishing, resizing and queued scroll events
    // must not produce another request without a new upward input.
    await page.waitForTimeout(150);
    expect(await loads(page)).toBe(index + 1);
  }
  await page.screenshot({ path: testInfo.outputPath('reader.png'), scale: 'css' });
});

test('restores a capped window even when the page lands before the next resize observation', async ({ page }) => {
  await open(page, '?count=300&delay=0');
  const height = await scroller(page).evaluate((el) => el.scrollHeight);
  for (let index = 0; index < 3; index += 1) {
    await scroller(page).evaluate((el) => { el.scrollTop = 0; });
    await expect(fixture(page)).toHaveAttribute('data-loads', String(index + 1));
    await expect.poll(() => scroller(page).evaluate((el) => el.scrollTop)).toBeGreaterThan(120);
    expect(await scroller(page).evaluate((el) => el.scrollHeight)).toBe(height);
    await page.waitForTimeout(150);
    expect(await loads(page)).toBe(index + 1);
  }
});

test('waits after an empty page and accepts another upward gesture at the hard top', async ({ page, isMobile }) => {
  await open(page, '?empty=1');
  await scroller(page).evaluate((el) => { el.scrollTop = 0; });
  await expect(fixture(page)).toHaveAttribute('data-loads', '1');
  await expect(page.getByRole('status', { name: copy('chat.loadingOlder') })).toHaveCount(0);
  await page.waitForTimeout(200);
  expect(await loads(page)).toBe(1);
  expect(await scroller(page).evaluate((el) => el.scrollTop)).toBe(0);
  if (isMobile) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 180, y: 200 }] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: 180, y: 300 }] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await cdp.detach();
  } else {
    await scroller(page).hover();
    await page.mouse.wheel(0, -150);
  }
  await expect(fixture(page)).toHaveAttribute('data-loads', '2');
  await expect(page.locator('[data-message-id]')).toHaveCount(100);
});

test('offers an explicit retry for a failed page and then restores the reader', async ({ page }) => {
  await open(page, '?fail=1');
  await scroller(page).evaluate((el) => { el.scrollTop = 0; });
  const retry = page.getByRole('button', { name: copy('chat.olderLoadFailed') });
  await expect(retry).toBeVisible();
  await page.waitForTimeout(200);
  expect(await loads(page)).toBe(1);
  await retry.click();
  await expect(page.locator('[data-message-id]')).toHaveCount(100);
  expect(await loads(page)).toBe(2);
  await expect.poll(() => scroller(page).evaluate((el) => el.scrollTop)).toBeGreaterThan(120);
});
