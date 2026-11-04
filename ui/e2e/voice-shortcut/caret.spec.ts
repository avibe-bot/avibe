import { expect, test } from '@playwright/test';
import en from '../../src/i18n/en.json' with { type: 'json' };

// A click on the empty composer is a click on its input, though a longer press of the same
// button records (ui/design: composer/hold-desktop/*). Until a press becomes a hold the browser
// keeps it: its mouseup and click reach the editor, as any click's do, and the caret shows where
// it landed. Checked past the moment the hold would have opened the microphone, after a quick
// click and after one just short of the hold.

// The composer's mouse hold (MOUSE_HOLD_GESTURE.holdMs).
const HOLD_MS = 500;

declare global {
  interface Window {
    caretTest: { mouseup: boolean | null; click: boolean | null };
  }
}

// No microphone here. The composer loads its audio worklet as it mounts, and that load can hang
// Linux WebKit's page (CI's stand-in for Safari's engine); the click under test needs no audio.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    if (typeof AudioWorklet !== 'undefined') AudioWorklet.prototype.addModule = async () => undefined;
  });
});

for (const [name, pressMs] of [['A click', 40], ['A slow click', HOLD_MS - 100]] as const) {
  test(`${name} on the empty composer leaves a visible caret in its input`, async ({ page }) => {
    await page.goto('/e2e/voice-shortcut/fixture.html');
    await expect(page.locator('[data-hold-to-talk="ready"]')).toBeVisible();
    const input = page.getByRole('textbox');
    const rect = (await input.boundingBox())!;
    const press = async (ms: number) => {
      await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height / 2);
      await page.mouse.down();
      await page.waitForTimeout(ms);
      await page.mouse.up();
    };
    // WebKit can deliver the first press of a fresh page a second late, which would carry a click
    // past the hold. One click first keeps that out of the measured one, made from a blurred input.
    await press(40);
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await expect(input).not.toBeFocused();
    await page.evaluate(() => {
      window.caretTest = { mouseup: null, click: null };
      const editor = document.querySelector('[contenteditable="true"]')!;
      for (const type of ['mouseup', 'click'] as const) {
        document.addEventListener(type, (event) => {
          window.caretTest[type] = editor.contains(event.target as Node);
        }, { capture: true, once: true });
      }
    });
    await press(pressMs);
    await page.waitForTimeout(HOLD_MS + 200);

    await expect(input).toBeFocused();
    expect(await page.evaluate(() => {
      const editor = document.querySelector('[contenteditable="true"]')!;
      const selection = window.getSelection()!;
      const caret = getComputedStyle(editor).caretColor;
      return {
        ...window.caretTest,
        collapsed: selection.rangeCount === 1 && selection.isCollapsed,
        inEditor: selection.anchorNode !== null && editor.contains(selection.anchorNode),
        caretShown: caret !== 'transparent' && !/^rgba\(.*,\s*0\)$/.test(caret),
      };
    })).toEqual({ mouseup: true, click: true, collapsed: true, inEditor: true, caretShown: true });
    // Nothing was recorded or noted.
    await expect(page.getByText(en.voice.composer.releaseToSend, { exact: true })).toHaveCount(0);
    await expect(page.getByRole('status').filter({ hasText: en.voice.composer.tooShort })).toHaveCount(0);
  });
}
