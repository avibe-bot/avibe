// Sheets, dialogs and popovers (docs/plans/web-ui-motion.md M6) in a real engine: what each plays as it
// opens and closes, on a phone and a desktop and under reduced motion, and dragging a phone sheet down.
import { expect, test, type CDPSession, type Locator, type Page } from '@playwright/test';

const DIALOG = '[role="dialog"]';
const SCRIM = 'div.motion-scrim';

/** The CSS animations an element is playing right now. */
const playing = (target: Locator) => target.evaluate((element) => element.getAnimations().map((animation) => (animation as CSSAnimation).animationName));
const transformOf = (target: Locator) => target.evaluate((element) => getComputedStyle(element).transform);

async function open(page: Page) {
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.goto('/e2e/motion/fixture.html');
  await expect(page.getByRole('button', { name: 'Open dialog' })).toBeVisible();
}

test('a dialog rises as a sheet on a phone and grows on a desktop, and leaves the way it came', async ({ page, isMobile }) => {
  await open(page);
  await page.getByRole('button', { name: 'Open dialog' }).click();
  const dialog = page.locator(DIALOG);
  await expect(dialog).toBeVisible();
  expect(await playing(dialog)).toEqual([isMobile ? 'motion-sheet-rise' : 'motion-grow-in']);
  expect(await playing(page.locator(SCRIM))).toEqual(['motion-fade-in']);
  await expect.poll(() => playing(dialog)).toEqual([]);
  await page.keyboard.press('Escape');
  expect(await playing(dialog)).toEqual([isMobile ? 'motion-sheet-sink' : 'motion-shrink-out']);
  await expect(dialog).toHaveCount(0);
});

test('fades a dialog, a menu and a popover in place under reduced motion', async ({ page, isMobile }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page);
  await page.getByRole('button', { name: 'Open dialog' }).click();
  expect(await playing(page.locator(DIALOG))).toEqual(['motion-fade-in']);
  await page.keyboard.press('Escape');
  await expect(page.locator(DIALOG)).toHaveCount(0);
  await page.getByRole('button', { name: 'Open menu' }).click();
  const menu = page.locator(isMobile ? DIALOG : '[data-radix-popper-content-wrapper] > *');
  expect(await playing(menu)).toEqual(['motion-fade-in']);
});

test('grows a popover from its trigger on a desktop', async ({ page, isMobile }) => {
  test.skip(isMobile, 'desktop popover');
  await open(page);
  await page.getByRole('button', { name: 'Open popover' }).click();
  const popover = page.getByText('Popover body');
  expect(await playing(popover)).toEqual(['motion-grow-in']);
  // From the trigger: Radix's origin, the popover's top edge, not its middle.
  expect(await popover.evaluate((element) => getComputedStyle(element).transformOrigin)).toMatch(/ 0px$/);
  await page.keyboard.press('Escape');
  expect(await playing(popover)).toEqual(['motion-fade-out']);
});

// ----- Dragging a phone sheet down (Chromium's touch emulation) -----

/** One finger from `from` down by each step of `path`, `stepMs` apart, then lifted. */
async function drag(cdp: CDPSession, page: Page, from: { x: number; y: number }, path: number[], stepMs: number) {
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: from.x, y: from.y }] });
  for (const dy of path) {
    await page.waitForTimeout(stepMs);
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: from.x, y: from.y + dy }] });
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}

const steps = (to: number, count: number) => Array.from({ length: count }, (_, i) => Math.round(((i + 1) / count) * to));

test.describe('dragging a phone sheet', () => {
  test.skip(({ isMobile, browserName }) => !isMobile || browserName !== 'chromium', 'touch emulation through Chromium');

  async function openSheet(page: Page) {
    await open(page);
    await page.getByRole('button', { name: 'Open dialog' }).click();
    const sheet = page.locator(DIALOG);
    await expect.poll(() => playing(sheet)).toEqual([]);
    const box = (await page.getByRole('heading', { name: 'Every Bot' }).boundingBox())!;
    return { sheet, grab: { x: box.x + 40, y: box.y + box.height / 2 }, cdp: await page.context().newCDPSession(page) };
  }

  test('springs back from a short, slow pull', async ({ page }) => {
    const { sheet, grab, cdp } = await openSheet(page);
    await drag(cdp, page, grab, steps(80, 8), 40);
    await page.waitForTimeout(500);
    await expect(sheet).toBeVisible();
    expect(await transformOf(sheet)).toBe('none');
  });

  test('dismisses past 30% of its height, from where the finger let it go', async ({ page }) => {
    const { sheet, grab, cdp } = await openSheet(page);
    const height = (await sheet.boundingBox())!.height;
    await drag(cdp, page, grab, steps(Math.round(height * 0.4), 10), 40);
    // Leaving from the finger's place, not from its resting one.
    const leaving = await sheet.evaluate((element) => ({ from: element.style.getPropertyValue('--sheet-drag'), animations: element.getAnimations().map((a) => (a as CSSAnimation).animationName) }));
    expect(leaving.animations).toEqual(['motion-sheet-sink']);
    expect(parseFloat(leaving.from)).toBeGreaterThan(height * 0.3);
    await expect(sheet).toHaveCount(0);
  });

  test('dismisses a quick flick however short', async ({ page }) => {
    const { sheet, grab, cdp } = await openSheet(page);
    // Each emulated touch takes Chromium tens of ms to deliver, so a flick is a few long strides.
    await drag(cdp, page, grab, steps(100, 2), 0);
    await expect(sheet).toHaveCount(0);
  });

  test('leaves content that is scrolled down to scroll', async ({ page }) => {
    const { sheet, cdp } = await openSheet(page);
    await sheet.evaluate((element) => { element.scrollTop = 400; });
    const row = (await page.getByText('Bot 12', { exact: true }).boundingBox())!;
    // Far enough that a drag would dismiss the sheet; here the content scrolls back up instead.
    await drag(cdp, page, { x: row.x + 40, y: row.y + row.height / 2 }, steps(240, 8), 12);
    await page.waitForTimeout(400);
    await expect(sheet).toBeVisible();
    expect(await transformOf(sheet)).toBe('none');
    expect(await sheet.evaluate((element) => element.scrollTop)).toBeLessThan(300);
  });
});
