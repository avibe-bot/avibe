import { expect, test, type Page } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

// Holding the empty composer to talk, with a real mouse in a real browser (ui/design:
// composer/hold-desktop/*): the fake microphone records, the fixture transcribes, and a release
// sends the words marked as spoken. A click, a right-click, leaving the box, and Esc do not.

declare global {
  interface Window {
    voiceShortcutTest: {
      sent: string[];
      spoken: { text: string; metadata: Record<string, unknown> }[];
      opened: number;
      posted: { session: string; text: string; metadata: Record<string, unknown> }[];
      transcribed: { reply?: string; user_previous?: string; vocabulary?: string[] }[];
      contextReads: string[];
    };
  }
}

const composer = async (page: Page) => {
  await page.goto('/e2e/voice-shortcut/fixture.html');
  const box = page.locator('[data-hold-to-talk="ready"]');
  await expect(box).toBeVisible();
  const rect = (await box.boundingBox())!;
  // Over the input, away from the box's buttons.
  return { box, rect, x: rect.x + rect.width * 0.4, y: rect.y + 22 };
};

test('a mouse hold on the empty composer records, and the release sends the words as spoken', async ({ page }) => {
  const { x, y } = await composer(page);
  await expect(page.getByRole('textbox')).toHaveAttribute('aria-label', en.voice.composer.placeholder);
  await page.mouse.move(x, y);
  await page.mouse.down();
  // The ring shows before the microphone opens.
  await expect(page.getByText(en.voice.composer.keepHolding)).toBeVisible();
  await expect(page.getByText('To “Fix the cache”')).toBeVisible();
  await expect(page.getByText(en.voice.composer.releaseToSend, { exact: true })).toBeVisible();
  await page.waitForTimeout(1_200);
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => window.voiceShortcutTest.spoken)).toEqual([
    { text: 'Send this transcript', metadata: { input: 'voice' } },
  ]);
  await expect(page.getByRole('textbox')).toHaveText('');
  // Heard in this conversation: its Agent's last reply and the user's previous message.
  expect(await page.evaluate(() => window.voiceShortcutTest.transcribed.at(-1))).toEqual({
    reply: 'The cache is fixed on prod-7.',
    user_previous: 'Is the cache fixed?',
    vocabulary: ['Vibey', 'Product'],
  });
});

test('a click only focuses, and a right-click keeps the system menu', async ({ page }) => {
  const { x, y } = await composer(page);
  await page.mouse.click(x, y);
  await expect(page.getByRole('textbox')).toBeFocused();
  // Listen first, then right-click, then read what the menu event was left with.
  await page.evaluate(() => {
    window.addEventListener('contextmenu', (event) => {
      setTimeout(() => { document.body.dataset.menuKept = String(!event.defaultPrevented); });
    }, { once: true });
  });
  await page.mouse.click(x, y, { button: 'right' });
  await expect(page.locator('body')).toHaveAttribute('data-menu-kept', 'true');
  await page.waitForTimeout(800);
  expect(await page.evaluate(() => window.voiceShortcutTest.sent)).toEqual([]);
});

test('leaving the box arms cancel, and a release there sends nothing', async ({ page }) => {
  const { rect, x, y } = await composer(page);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.waitForTimeout(900);
  await page.mouse.move(x, rect.y - 60, { steps: 4 });
  await expect(page.getByText(en.voice.composer.mouseArmed)).toBeVisible();
  await page.mouse.up();
  await expect(page.getByText(en.voice.cancelled)).toBeVisible();
  await page.waitForTimeout(600);
  expect(await page.evaluate(() => window.voiceShortcutTest.sent)).toEqual([]);
});

test('Esc cancels a hold at once', async ({ page }) => {
  const { x, y } = await composer(page);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.waitForTimeout(900);
  await page.keyboard.press('Escape');
  await expect(page.getByText(en.voice.cancelled)).toBeVisible();
  await page.mouse.up();
  await page.waitForTimeout(600);
  expect(await page.evaluate(() => window.voiceShortcutTest.sent)).toEqual([]);
});

test('a composer with text gives the press back to the system', async ({ page }) => {
  const { x, y } = await composer(page);
  await page.getByRole('textbox').click();
  await page.keyboard.type('typed words');
  await expect(page.locator('[data-hold-to-talk="ready"]')).toHaveCount(0);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.waitForTimeout(900);
  await expect(page.getByText(en.voice.composer.releaseToSend, { exact: true })).toHaveCount(0);
  await page.mouse.up();
});
