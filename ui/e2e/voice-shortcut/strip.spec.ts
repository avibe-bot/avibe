import { expect, test, type Locator, type Page } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

// Holding a Bot's avatar in the Bots strip to talk to that Bot (docs/plans/bots.md D5), with a real
// mouse and real touches in a real browser: the fake microphone records, the fixture transcribes,
// and a release posts the words, marked as spoken, to the held Bot's conversation. A click opens
// the conversation, a right-click opens the menu, leaving the avatar or Esc cancels, a finger slides
// up to cancel, and a sideways swipe scrolls the strip without starting a hold.

const strip = async (page: Page) => {
  await page.goto('/e2e/voice-shortcut/fixture.html?surface=strip');
  const region = page.getByRole('region', { name: en.bots.sectionLabel });
  await expect(region.getByRole('button', { name: 'Product' })).toBeVisible();
  return region;
};

const centerOf = async (target: Locator) => {
  const box = (await target.boundingBox())!;
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
};

const posted = (page: Page) => page.evaluate(() => window.voiceShortcutTest.posted);
const opened = (page: Page) => page.locator('[data-opened]').getAttribute('data-opened');

test.describe('with a mouse', () => {
  test('holding an avatar speaks to that Bot, a click opens its conversation, and a right-click opens its menu', async ({ page }) => {
    const region = await strip(page);
    const pm = region.getByRole('button', { name: 'Product' });
    const at = await centerOf(pm);

    await page.mouse.move(at.x, at.y);
    await page.mouse.down();
    const card = page.getByRole('dialog', { name: 'To Product' });
    await expect(card).toBeVisible();
    await expect(page.getByText('Release to send · move off the avatar or press')).toBeVisible();
    await page.waitForTimeout(1_200);
    await page.mouse.up();
    await expect.poll(() => posted(page)).toEqual([
      { session: 'ses_pm', text: 'Send this transcript', metadata: { input: 'voice' } },
    ]);
    // The reader stays where they are; the toast's View opens the conversation.
    const toast = page.getByRole('status').filter({ hasText: 'Sent to Product' });
    await expect(toast).toBeVisible();
    expect(await opened(page)).toBe('/');
    await toast.getByRole('button', { name: en.voice.view }).click();
    await expect.poll(() => opened(page)).toBe('/chat/ses_pm');

    // A click is a click: the conversation opens and nothing is recorded.
    await region.getByRole('button', { name: 'Research' }).click();
    await expect.poll(() => opened(page)).toBe('/chat/ses_research');
    // A right-click is the menu, never a hold.
    await region.getByRole('button', { name: 'Writer' }).click({ button: 'right' });
    await expect(page.getByRole('menu').getByRole('menuitem', { name: en.bots.menu.editProfile })).toBeVisible();
    await page.waitForTimeout(800);
    await expect(page.getByRole('dialog', { name: 'To Writer' })).toHaveCount(0);
    expect(await posted(page)).toHaveLength(1);
  });

  test('leaving the avatar arms cancel and a release there sends nothing; Esc cancels at once', async ({ page }) => {
    const region = await strip(page);
    const at = await centerOf(region.getByRole('button', { name: 'Product' }));

    await page.mouse.move(at.x, at.y);
    await page.mouse.down();
    await expect(page.getByRole('dialog', { name: 'To Product' })).toBeVisible();
    await page.waitForTimeout(700);
    await page.mouse.move(at.x + 120, at.y + 60, { steps: 4 });
    await expect(page.getByText(en.voice.avatar.mouseArmed)).toBeVisible();
    await page.mouse.up();
    await expect(page.getByRole('status').filter({ hasText: en.voice.cancelled })).toBeVisible();

    await page.mouse.move(at.x, at.y);
    await page.mouse.down();
    await expect(page.getByRole('dialog', { name: 'To Product' })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: 'To Product' })).toHaveCount(0);
    await page.mouse.up();
    await page.waitForTimeout(500);
    expect(await posted(page)).toEqual([]);
  });
});

test('a take the system interrupts keeps its words, and Send posts them to that Bot', async ({ page }) => {
  const region = await strip(page);
  const at = await centerOf(region.getByRole('button', { name: 'Product' }));

  await page.mouse.move(at.x, at.y);
  await page.mouse.down();
  await expect(page.getByRole('dialog', { name: 'To Product' })).toBeVisible();
  await page.waitForTimeout(1_000);
  // The window loses focus mid-take (another app, a notification): nothing is sent or lost.
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  const card = page.getByRole('dialog', { name: 'To Product' });
  await expect(card).toContainText('Send this transcript');
  await page.mouse.up();
  expect(await posted(page)).toEqual([]);
  await card.getByRole('button', { name: en.voice.send }).click();
  await expect.poll(() => posted(page)).toEqual([
    { session: 'ses_pm', text: 'Send this transcript', metadata: { input: 'voice' } },
  ]);
});

test.describe('with a finger', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  /** Real touch input: `moves` are the points the finger passes through after `holdMs`. */
  async function touch(page: Page, from: { x: number; y: number }, holdMs: number, moves: { x: number; y: number }[] = []) {
    const cdp = await page.context().newCDPSession(page);
    const send = (type: 'touchStart' | 'touchMove' | 'touchEnd', at?: { x: number; y: number }) => cdp.send('Input.dispatchTouchEvent', {
      type,
      touchPoints: at ? [{ x: at.x, y: at.y, id: 1 }] : [],
    });
    await send('touchStart', from);
    await page.waitForTimeout(holdMs);
    for (const point of moves) {
      await send('touchMove', point);
      await page.waitForTimeout(40);
    }
    if (moves.length) await page.waitForTimeout(300);
    await send('touchEnd');
    await cdp.detach();
  }

  test('holding an avatar speaks to that Bot, and sliding up cancels', async ({ page }) => {
    const region = await strip(page);
    const at = await centerOf(region.getByRole('button', { name: 'Product' }));

    await touch(page, at, 1_200);
    await expect.poll(() => posted(page)).toEqual([
      { session: 'ses_pm', text: 'Send this transcript', metadata: { input: 'voice' } },
    ]);
    await expect(page.getByRole('status').filter({ hasText: 'Sent to Product' })).toBeVisible();

    await touch(page, at, 1_000, [1, 2, 3, 4, 5, 6].map((step) => ({ x: at.x, y: at.y - step * 20 })));
    await expect(page.getByRole('status').filter({ hasText: en.voice.cancelled })).toBeVisible();
    expect(await posted(page)).toHaveLength(1);
  });

  test('a sideways swipe scrolls the strip and starts no hold', async ({ page }) => {
    const region = await strip(page);
    const scroller = region.locator('.bot-strip-scroller');
    const at = await centerOf(region.getByRole('button', { name: 'Product' }));

    await touch(page, at, 30, [1, 2, 3, 4, 5, 6].map((step) => ({ x: at.x - step * 25, y: at.y })));
    await expect.poll(() => scroller.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
    await page.waitForTimeout(700);
    await expect(page.getByRole('dialog', { name: 'To Product' })).toHaveCount(0);
    expect(await posted(page)).toEqual([]);
    expect(await opened(page)).toBe('/');
  });
});
