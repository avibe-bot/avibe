import { expect, test } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

declare global {
  interface Window {
    voiceShortcutTest: {
      sent: string[];
    };
  }
}

test('shortcut completion restores the transcript caret and Enter sends', async ({ page }) => {
  await page.goto('/e2e/voice-shortcut/fixture.html');
  const textbox = page.getByRole('textbox');
  await textbox.focus();
  await page.keyboard.press('Alt+z');
  await expect(page.getByRole('button', { name: en.chat.compose.stopRecording })).toBeVisible();

  await page.waitForTimeout(1_100);
  await page.keyboard.press('Alt+z');

  await expect(textbox).toHaveText('Send this transcript');
  await expect(textbox).toBeFocused();
  await expect.poll(() => textbox.evaluate((element) => {
    const selection = window.getSelection();
    return {
      active: document.activeElement === element,
      text: selection?.toString() ?? '',
      anchorOffset: selection?.anchorOffset ?? -1,
      focusOffset: selection?.focusOffset ?? -1,
    };
  })).toEqual({
    active: true,
    text: '',
    anchorOffset: 'Send this transcript'.length,
    focusOffset: 'Send this transcript'.length,
  });

  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => window.voiceShortcutTest.sent)).toEqual([
    'Send this transcript',
  ]);
});
