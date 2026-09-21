import { expect, test, type Locator, type Page } from '@playwright/test';
import { VIEWPORTS, serveProduct, settleEffects, size } from './support';
import { importableCount, openProviders, serveProviders, vendorCount } from './provider-support';

/**
 * The four things about the providers screen that only a browser can settle.
 *
 * Its component tests already hold the rules that are decidable from the DOM — which
 * rows a batch submits, which card is selectable, what a switch preserves. What they
 * cannot see is layout: whether a frame really holds still, whether a capsule really
 * leaves the button where it was, whether anything spills sideways at the sizes the
 * design bands for. Those are measurements, so they are made here.
 *
 * And one thing that is not layout at all: the numbers. The badge, the summary and the
 * capsule each count a different set, and the interesting failure is not that one of
 * them is wrong in isolation but that they disagree — the badge counting an import, or
 * the summary counting a card rather than a source. So every count below is compared
 * against what the fake server actually holds, never against a literal. A test that
 * asserted "2 providers" would keep passing on a screen that had stopped counting.
 */

const round = (value: number) => Math.round(value * 100) / 100;

/** A box, rounded, so a sub-pixel difference in two reads of a still frame is not a
 *  failure while a real one-pixel move still is. */
const rect = async (locator: Locator) => {
  const box = await locator.boundingBox();
  if (!box) throw new Error('not rendered');
  return { x: round(box.x), y: round(box.y), width: round(box.width), height: round(box.height) };
};

/** Every number in a sentence, in order — how a count is read back out of copy
 *  without the test restating the copy. */
const numbers = (text: string | null): number[] =>
  [...(text ?? '').matchAll(/\d+/g)].map((match) => Number(match[0]));

const method = (page: Page, name: string) =>
  page.locator('.setup-add-method').filter({ hasText: name });

const primaryAction = (page: Page) => page.locator('.onboarding-primary-action');

const addDialog = (page: Page) => page.locator('.setup-add-dialog');

/** Holds whatever is arriving. Radix animates the dialog in, and the stage plays its
 *  wire sequence; a box measured mid-entrance is a box of something still moving. */
const arrived = async (locator: Locator) => {
  await locator.evaluate((node) => Promise.all(
    node.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => null)),
  ));
};

const openAddDialog = async (page: Page) => {
  await page.locator('.setup-provider-card[data-state="add"]').click();
  await addDialog(page).waitFor();
  await arrived(addDialog(page));
};

test.describe('providers screen', () => {
  test('C5: the add dialog keeps one frame across repeated method switching', async ({ page }) => {
    const denied = await serveProduct(page);
    await serveProviders(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openProviders(page);
    await openAddDialog(page);

    // Three methods exist here only because the fixture leaves one detected candidate
    // off the stage. Asserting that first means a later failure to switch is read as a
    // failure to switch, not as a method that was never offered.
    await expect(page.locator('.setup-add-method')).toHaveCount(3);

    const anchors = {
      dialog: await rect(addDialog(page)),
      head: await rect(page.locator('.setup-add-head')),
      foot: await rect(page.locator('.setup-add-foot')),
    };
    const contentHeights: number[] = [];

    // Back and forth, not once through: the frame that survives one switch and drifts
    // on the third is the one a person actually comparing two methods would meet.
    for (const [name, expected] of [
      ['API Key', 'apiKey'],
      ['Subscription', 'subscription'],
      ['Detected', 'detected'],
      ['API Key', 'apiKey'],
      ['Detected', 'detected'],
      ['Subscription', 'subscription'],
    ] as const) {
      await method(page, name).click();
      await expect(page.locator('.setup-add-body')).toHaveAttribute('data-method', expected);

      expect(await rect(addDialog(page))).toEqual(anchors.dialog);
      expect(await rect(page.locator('.setup-add-head'))).toEqual(anchors.head);
      expect(await rect(page.locator('.setup-add-foot'))).toEqual(anchors.foot);

      contentHeights.push(await page.locator('.setup-add-body').evaluate((node) => node.scrollHeight));
    }

    // And the frame held still because it is sized by the rule rather than by what is
    // in it: the panes genuinely differ in height, so a content-fitted box could not
    // have produced the identical rects above.
    expect(new Set(contentHeights).size).toBeGreaterThan(1);
    expect(denied).toEqual([]);
  });

  test('the badge, the summary and the capsule each count their own set', async ({ page }) => {
    const denied = await serveProduct(page);
    const server = await serveProviders(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openProviders(page);

    const summary = page.locator('.setup-provider-summary');
    const capsule = page.locator('.onboarding-import-notice-text');
    const badge = page.locator('.setup-provider-badge');

    // ── Nothing yet ─────────────────────────────────────────────────────────
    expect(vendorCount(server.facts())).toBe(0);
    await expect(badge).toHaveCount(0);
    // Three importable keys on the machine, and the capsule says so.
    expect(numbers(await capsule.textContent())).toEqual([importableCount(server.facts())]);

    // ── An import ───────────────────────────────────────────────────────────
    // Every proposed row arrives consented to, as the shipped takeover opens it.
    await expect(primaryAction(page)).toContainText(String(importableCount(server.facts())));

    // Withdrawing one card withdraws that provider's rows and nothing else.
    await page.locator('.setup-provider-card[data-provider="openai"]').click();
    await expect(primaryAction(page)).toContainText('2');
    await primaryAction(page).click();

    const takeover = page.getByRole('dialog');
    await takeover.getByRole('button', { name: 'Start migration' }).click();
    await expect(takeover.getByRole('button', { name: 'Done', exact: true })).toBeVisible();

    // One atomic batch holding exactly the consented rows — the withdrawn provider's
    // key was never in it, and nothing was submitted a second time.
    expect(server.facts().applied).toHaveLength(1);
    expect([...server.facts().applied[0]].sort()).toEqual(['opencode-qwen', 'opencode-zhipu']);

    await takeover.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);

    const imported = server.facts().applied.flat().length;
    // The summary counts providers that EXIST, so an import moves it.
    await expect.poll(async () => numbers(await summary.textContent())[0])
      .toBe(vendorCount(server.facts()));
    // The capsule reports what landed and what the refreshed scan still holds.
    await expect.poll(async () => numbers(await capsule.textContent()))
      .toEqual([imported, importableCount(server.facts())]);
    // The badge counts what was added THROUGH 「Add more」, and an import is not that.
    // A badge that moved here would be counting sources.
    await expect(badge).toHaveCount(0);

    // ── A write ─────────────────────────────────────────────────────────────
    await openAddDialog(page);
    await method(page, 'API Key').click();
    const body = page.locator('.setup-add-body');
    await body.getByLabel('Base URL').fill('https://proxy.example/v1');
    // Exactly, because the reveal toggle beside it is labelled 「Show API key」.
    await body.getByLabel('API key', { exact: true }).fill('sk-e2e-provider');
    await page.locator('.setup-add-foot button').last().click();
    await expect(addDialog(page)).toHaveCount(0);

    // Now the badge moves, because this one did come through 「Add more」…
    await expect.poll(async () => numbers(await badge.textContent())[0])
      .toBe(server.facts().written.length);
    // …and the summary moves with it, still counting every provider that exists
    // rather than only the two the stage draws.
    await expect.poll(async () => numbers(await summary.textContent())[0])
      .toBe(vendorCount(server.facts()));
    expect(vendorCount(server.facts())).toBeGreaterThan(server.facts().written.length);
    expect(denied).toEqual([]);
  });

  test('dismissing the import capsule leaves the primary action where it was', async ({ page }) => {
    const denied = await serveProduct(page);
    await serveProviders(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openProviders(page);
    await page.locator('.onboarding-import-notice').waitFor();
    await settleEffects(page);

    const before = { slot: await rect(page.locator('.setup-provider-offer')), action: await rect(primaryAction(page)) };

    await page.getByRole('button', { name: 'Dismiss import notice' }).click();
    await expect(page.locator('.onboarding-import-notice')).toHaveCount(0);
    await settleEffects(page);

    // The offer is an optional sentence in a slot that is not optional. Letting the
    // slot collapse would pull the button someone was about to press up under their
    // cursor — which is the whole reason the slot is reserved rather than conditional.
    expect(await rect(page.locator('.setup-provider-offer'))).toEqual(before.slot);
    expect((await rect(primaryAction(page))).y).toBe(before.action.y);
    expect(denied).toEqual([]);
  });

  // The bands the stylesheet declares, plus the two ends of the range. Taken from the
  // shared table rather than retyped, so a size the suite stops covering is visible as
  // a size the suite stops covering.
  const OVERFLOW_SIZES = VIEWPORTS.filter((viewport) => [1920, 1440, 1366, 390, 320].includes(viewport.width));

  for (const viewport of OVERFLOW_SIZES) {
    test(`nothing spills sideways at ${size(viewport)}`, async ({ page }) => {
      const denied = await serveProduct(page);
      await serveProviders(page);
      await page.setViewportSize(viewport);
      await openProviders(page);
      await settleEffects(page);

      const horizontal = () => page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));

      const stage = await horizontal();
      expect(stage.scrollWidth).toBeLessThanOrEqual(stage.clientWidth + 1);

      // The dialog is the harder case: it is fixed, so a box wider than the window
      // would not show up as document overflow at all — it would simply be cut off,
      // with its footer buttons somewhere off the right edge.
      await openAddDialog(page);
      await settleEffects(page);

      const dialog = await rect(addDialog(page));
      const window = await page.evaluate(() => globalThis.innerWidth);
      expect(dialog.x).toBeGreaterThanOrEqual(-1);
      expect(dialog.x + dialog.width).toBeLessThanOrEqual(window + 1);

      const withDialog = await horizontal();
      expect(withDialog.scrollWidth).toBeLessThanOrEqual(withDialog.clientWidth + 1);
      expect(denied).toEqual([]);
    });
  }
});
