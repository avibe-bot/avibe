// Toasts (docs/plans/web-ui-motion.md M9) in a real engine: a toast rises into the stack, the stack lifts
// to make room, a toast leaves before it is gone, and a finger swipes one away on a phone.
import { expect, test, type Page } from '@playwright/test';

const toasts = (page: Page) => page.getByRole('status');
/** What each running Web Animation on an element moves: `transform`, `opacity`, or both. */
const moving = (page: Page, index: number) => toasts(page).nth(index).evaluate((element) => element.getAnimations().map((animation) => {
  const frames = (animation.effect as KeyframeEffect).getKeyframes();
  return ['transform', 'opacity'].filter((property) => frames.some((frame) => property in frame)).join('+');
}).sort());

async function open(page: Page) {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/motion/fixture.html');
  await expect(page.getByRole('button', { name: 'Toast' })).toBeVisible();
}

test('a toast rises into the stack, which lifts to make room', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: 'Toast' }).click();
  await expect(toasts(page)).toHaveCount(1);
  expect(await moving(page, 0)).toEqual(['opacity', 'transform']);
  await page.waitForTimeout(600);
  await page.getByRole('button', { name: 'Toast' }).click();
  await expect(toasts(page)).toHaveCount(2);
  // The first glides up from where it was; the new one rises and fades in below it, one beat later, so
  // the two tinted cards never cross.
  expect(await moving(page, 0)).toEqual(['transform']);
  expect(await moving(page, 1)).toEqual(['opacity', 'transform']);
  const beat = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--motion-beat')));
  expect(await toasts(page).nth(1).evaluate((element) => element.getAnimations().map((animation) => animation.effect!.getTiming().delay))).toEqual([beat, beat]);
});

test('a dismissed toast fades and drops before it is gone', async ({ page }) => {
  await open(page);
  await page.getByRole('button', { name: 'Toast' }).click();
  await page.waitForTimeout(600);
  await toasts(page).getByRole('button').last().click();
  await expect(toasts(page)).toHaveCount(1);
  expect(await moving(page, 0)).toEqual(['transform+opacity']);
  await expect(toasts(page)).toHaveCount(0);
});

test('only fades toasts under reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page);
  await page.getByRole('button', { name: 'Toast' }).click();
  expect(await moving(page, 0)).toEqual(['opacity']);
  await page.waitForTimeout(400);
  await page.getByRole('button', { name: 'Toast' }).click();
  await expect(toasts(page)).toHaveCount(2);
  expect(await moving(page, 0)).toEqual([]);
});

test('a swipe that stops before letting go keeps the toast', async ({ page, isMobile, browserName }) => {
  test.skip(!isMobile || browserName !== 'chromium', 'touch emulation through Chromium');
  await open(page);
  await page.getByRole('button', { name: 'Toast' }).click();
  await page.waitForTimeout(600);
  const box = (await toasts(page).first().boundingBox())!;
  const cdp = await page.context().newCDPSession(page);
  const y = box.y + box.height / 2;
  const x = box.x + 20;
  // A quick 20% of its width, then held still before letting go.
  const dx = Math.round(box.width * 0.2);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + 10, y }] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx, y }] });
  await page.waitForTimeout(400);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(500);
  await expect(toasts(page)).toHaveCount(1);
});

test('a finger swipes a toast away sideways on a phone', async ({ page, isMobile, browserName }) => {
  test.skip(!isMobile || browserName !== 'chromium', 'touch emulation through Chromium');
  await open(page);
  await page.getByRole('button', { name: 'Toast' }).click();
  await page.waitForTimeout(600);
  const box = (await toasts(page).first().boundingBox())!;
  const cdp = await page.context().newCDPSession(page);
  const y = box.y + box.height / 2;
  const x = box.x + 20;
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  for (let dx = 15; dx <= 150; dx += 15) {
    await page.waitForTimeout(16);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx, y }] });
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await expect(toasts(page)).toHaveCount(0, { timeout: 1000 });
});
