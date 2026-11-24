import { expect, test, type Page } from '../support/browserTest';

// An opened execution-process group offers the running card's close control (the X that turns Agent
// Activity off), between its eye pill and its chevron. On a phone the header's label gives way first:
// the controls stay whole inside the header and take the press.

const transcript = (page: Page) => page.getByTestId('chat-transcript');
const CHIP = '[data-transcript-key^="activity:"]';
// The fixture's conversation is in Chinese (src/i18n/zh.json chat.agentActivity.disable / .collapse).
const CLOSE = '关闭 Agent 执行过程显示';
const COLLAPSE = '收起执行过程';

async function open(page: Page, width: number) {
  // No request from this page may reach a running Avibe service.
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, json: { error: 'No backend in this fixture' } }));
  await page.setViewportSize({ width, height: 700 });
  await page.goto('/e2e/chat-scroll/fixture.html?images=0&delay=0&activity=1&records=1&count=40');
  await expect(transcript(page).locator('[data-message-id]').first()).toBeVisible();
  await expect(page.locator('[contenteditable="true"]').first()).toBeAttached();
}

for (const width of [360, 390]) {
  test(`an opened group's header keeps the close control whole and pressable at ${width} px`, async ({ page }) => {
    await open(page, width);
    const chip = transcript(page).locator(CHIP).last();
    await chip.scrollIntoViewIfNeeded();
    // The closed pill stays one control.
    await expect(chip.getByRole('button', { name: CLOSE })).toHaveCount(0);
    await chip.locator(':scope > div > button').click();
    // A label too long for the header (a long turn, a large count, a longer language): spaced out here,
    // so the label must give way while the controls stay whole.
    await page.addStyleTag({ content: `${CHIP} [data-transcript-summary-head] [data-transcript-shared="label"] { letter-spacing: 1.5em; }` });
    const close = chip.getByRole('button', { name: CLOSE });
    await expect(close).toBeVisible();
    await page.waitForTimeout(400);

    const layout = await chip.evaluate((root, close) => {
      const head = root.querySelector('[data-transcript-summary-head]')!;
      const box = (el: Element) => el.getBoundingClientRect();
      const [label, eye, x, chevron] = [...head.children].map(box);
      const xButton = root.querySelector(`button[aria-label="${close}"]`)!;
      const centre = box(xButton);
      const text = head.querySelector('[data-transcript-shared="label"]')!;
      return {
        truncated: text.scrollWidth > text.clientWidth,
        head: box(head),
        label, eye, x, chevron,
        order: [...head.children].map((el) => el.getAttribute('aria-label') ?? el.tagName),
        hit: xButton.contains(document.elementFromPoint(centre.x + centre.width / 2, centre.y + centre.height / 2)),
      };
    }, CLOSE);
    // Eye pill, X, chevron, in that order, each whole inside the header; the label ends before them.
    expect(layout.order.slice(2)).toEqual([CLOSE, COLLAPSE]);
    expect(layout.truncated).toBe(true);
    expect(layout.label.right).toBeLessThanOrEqual(layout.eye.left);
    expect(layout.eye.right).toBeLessThanOrEqual(layout.x.left);
    expect(layout.x.right).toBeLessThanOrEqual(layout.chevron.left);
    expect(layout.chevron.right).toBeLessThanOrEqual(layout.head.right);
    expect(layout.x.width).toBe(24);
    expect(layout.hit).toBe(true);

    // The press turns Agent Activity off: the groups leave the conversation.
    await close.click();
    await expect(transcript(page).locator(CHIP)).toHaveCount(0);
  });
}
