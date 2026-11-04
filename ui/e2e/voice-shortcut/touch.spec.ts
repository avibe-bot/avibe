import { expect, test, type Locator, type Page } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

// The toast a hold leaves, with a finger on a phone (ui/design: composer/hold-mobile/*,
// mobile/main-bot-voice/*): the press and the slide are real touches, and so is the tap on the
// toast's close. Both ways in are checked: the composer and the tab bar's main Bot.

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

/** A finger held on `at`, then slid `slideUp` px up, then lifted: real touch input. The slide is
 *  a finger's drag to the cancel target (about 400 px/s) that rests there before lifting, as the
 *  target turns red. A flick would leave a fling behind, and Chromium swallows the tap that
 *  stops a fling (on Linux), which no tap a reader makes on the toast afterwards would meet. */
async function holdWithFinger(page: Page, at: { x: number; y: number }, holdMs: number, slideUp = 0) {
  const cdp = await page.context().newCDPSession(page);
  const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', y: number) => cdp.send('Input.dispatchTouchEvent', {
    type,
    touchPoints: type === 'touchEnd' ? [] : [{ x: at.x, y, id: 1 }],
  });
  await touch('touchStart', at.y);
  await page.waitForTimeout(holdMs);
  for (let step = 1; step <= 6 && slideUp; step += 1) {
    await touch('touchMove', at.y - (slideUp * step) / 6);
    await page.waitForTimeout(50);
  }
  if (slideUp) await page.waitForTimeout(300);
  await touch('touchEnd', at.y - slideUp);
  await cdp.detach();
}

/** A finger's tap at the middle of `target`. */
async function tap(page: Page, target: Locator) {
  const box = (await target.boundingBox())!;
  // A fingertip-sized target (44 pt), not just a glyph.
  expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(44);
  await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
}

const centerOf = async (target: Locator) => {
  const box = (await target.boundingBox())!;
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
};

test('the composer: a take slid away to cancel leaves a toast whose close a finger can tap', async ({ page }) => {
  await page.goto('/e2e/voice-shortcut/fixture.html');
  const box = page.locator('[data-hold-to-talk="ready"]');
  await expect(box).toBeVisible();
  const rect = (await box.boundingBox())!;
  await holdWithFinger(page, { x: rect.x + rect.width * 0.4, y: rect.y + 22 }, 1_000, 120);

  const toast = page.getByRole('status').filter({ hasText: en.voice.cancelled });
  await expect(toast).toBeVisible();
  await tap(page, toast.getByRole('button', { name: en.voice.dismiss }));
  // Well before the toast's own timeout.
  await expect(toast).toBeHidden({ timeout: 1_000 });
  expect(await page.evaluate(() => window.voiceShortcutTest.sent)).toEqual([]);
});

test('the main Bot: the cancelled toast closes on a tap, and the sent toast opens the conversation', async ({ page }) => {
  await page.goto('/e2e/voice-shortcut/fixture.html?surface=center');
  const center = page.getByRole('button', { name: 'Open Vibey' });
  await expect(center).toBeVisible();

  await holdWithFinger(page, await centerOf(center), 1_000, 120);
  const cancelled = page.getByRole('status').filter({ hasText: en.voice.cancelled });
  await expect(cancelled).toBeVisible();
  await tap(page, cancelled.getByRole('button', { name: en.voice.dismiss }));
  await expect(cancelled).toBeHidden({ timeout: 1_000 });

  await holdWithFinger(page, await centerOf(center), 1_200);
  const sent = page.getByRole('status').filter({ hasText: 'Send this transcript' });
  await expect(sent).toBeVisible();
  await tap(page, sent.getByRole('button', { name: en.voice.view }));
  await expect(sent).toBeHidden({ timeout: 1_000 });
  expect(await page.evaluate(() => window.voiceShortcutTest)).toMatchObject({ sent: ['Send this transcript'], opened: 1 });
});
