import { expect, test } from '@playwright/test';

import { DESKTOP, NARROW, WIDE, open, serveProduct } from './support';

/**
 * The packet's geometry claims, measured in a browser rather than inferred from
 * class names. Two of them are fixed numbers (sidebar 248, settings rail 196)
 * and the rest are the opposite claim — that the content beside them is fluid,
 * which a capture at the 1200 staging width alone cannot distinguish from a
 * column that happens to be 856 or 924 wide.
 */

const SIDEBAR = 'aside.fixed';
const SHELL_CONTENT = 'main#app-shell-scroll > div';
const SETTINGS_RAIL = 'nav[aria-label="Settings sections"]';
const SETTINGS_CONTENT = 'nav[aria-label="Settings sections"] + section > div';
const APPEARANCE_CARD = 'section.bg-surface-2:has([role="radiogroup"])';
const ULTRA = { width: 1920, height: 1000 };

const widthOf = async (page: import('@playwright/test').Page, selector: string) => {
  const box = await page.locator(selector).first().boundingBox();
  expect(box, `${selector} should be laid out`).not.toBeNull();
  return box!.width;
};

test.describe('workbench home geometry', () => {
  test('keeps the sidebar at 248 and lets everything beside it grow', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/');
    await expect(page.locator(SIDEBAR)).toBeVisible();

    expect(await widthOf(page, SIDEBAR)).toBe(248);
    const atStaging = await widthOf(page, SHELL_CONTENT);

    await page.setViewportSize(WIDE);
    await page.waitForFunction(() => window.innerWidth === 1600);

    // The sidebar is the fixed part; the column beside it is not. It has to take
    // the full extra 400, because a cap of any size would swallow some of it.
    expect(await widthOf(page, SIDEBAR)).toBe(248);
    const atWide = await widthOf(page, SHELL_CONTENT);
    expect(atWide - atStaging).toBe(WIDE.width - DESKTOP.width);

    // eslint-disable-next-line no-console
    console.log(`home content: ${atStaging} @1200, ${atWide} @1600`);
    expect(denied).toEqual([]);
  });

  test('drops the sidebar on a phone and still fills the width', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(NARROW);
    await open(page, '/');

    await expect(page.locator(SIDEBAR)).toBeHidden();
    expect(await widthOf(page, SHELL_CONTENT)).toBe(NARROW.width);
  });
});

test.describe('general settings geometry', () => {
  test('keeps the rail at 196 and keeps the page fluid past the shared cap', async ({ page }) => {
    const denied = await serveProduct(page);

    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);
    const cardAtStaging = await widthOf(page, APPEARANCE_CARD);

    // Wide enough to pass the shared 1180 reading column on purpose. Measuring
    // below it could not tell a fluid page from a capped one, which is the whole
    // question: the source draws General as content that fills whatever the rail
    // leaves, so a cap of any size — inherited or not — contradicts it.
    await page.setViewportSize(ULTRA);
    await page.waitForFunction(() => window.innerWidth === 1920);

    expect(await widthOf(page, SETTINGS_RAIL)).toBe(196);
    const cardAtUltra = await widthOf(page, APPEARANCE_CARD);
    expect(cardAtUltra).toBeGreaterThan(1180);
    expect(cardAtUltra - cardAtStaging).toBe(ULTRA.width - DESKTOP.width);

    // eslint-disable-next-line no-console
    console.log(`general card: ${cardAtStaging} @1200, ${cardAtUltra} @1920`);
    expect(denied).toEqual([]);
  });

  test('leaves the shared reading column on every other settings page', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(ULTRA);
    await open(page, '/settings/shortcuts');
    await expect(page.locator(SETTINGS_RAIL)).toBeVisible();

    // General opts out by route, so the pages that wanted the reading column
    // still have it at a width where the difference is visible.
    expect(await widthOf(page, SETTINGS_CONTENT)).toBe(1180);
  });

  test('draws the preference card, the selector and the selected choice to spec', async ({ page }) => {
    await serveProduct(page);
    await page.setViewportSize(DESKTOP);
    await open(page, '/settings/general', { theme: 'dark' });

    const card = page.locator(APPEARANCE_CARD);
    await expect(card).toBeVisible();

    const cardStyle = await card.evaluate((node) => {
      const outer = getComputedStyle(node);
      const inner = getComputedStyle(node.firstElementChild as Element);
      return { radius: outer.borderRadius, pad: inner.padding, gap: inner.rowGap, bg: outer.backgroundColor };
    });
    // le5QU / Q8zxF1: padding 22, radius 12 — not the 16 the older `panel`
    // variant renders, which is why `preference` is its own variant.
    expect(cardStyle.radius).toBe('12px');
    expect(cardStyle.pad).toBe('22px');
    expect(cardStyle.gap).toBe('20px');

    const surface2 = await page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.backgroundColor = 'var(--surface-2)';
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return value;
    });
    expect(cardStyle.bg).toBe(surface2);

    const select = page.getByLabel('Language', { exact: true });
    const selectBox = await select.boundingBox();
    expect(selectBox!.width).toBe(190);
    expect(selectBox!.height).toBe(40);
    expect(await select.evaluate((node) => getComputedStyle(node).borderRadius)).toBe('9px');

    const dark = page.getByRole('radio', { name: 'Dark' });
    await expect(dark).toHaveAttribute('aria-checked', 'true');
    const selectedBorder = await dark.evaluate((node) => {
      const style = getComputedStyle(node);
      return { width: style.borderTopWidth, color: style.borderTopColor };
    });
    expect(selectedBorder.width).toBe('2px');

    const mint = await page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.color = 'var(--mint)';
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    });
    expect(selectedBorder.color).toBe(mint);

    // Picking a card must not nudge the row: the extra border pixel is absorbed
    // by padding, so every card in the group stays the same size.
    const boxes = await page.getByRole('radio').evaluateAll((nodes) =>
      nodes.map((node) => node.getBoundingClientRect().width));
    expect(new Set(boxes.map((w) => Math.round(w))).size).toBe(1);

    // Q8zxF1: a 14 gutter between the three choices, and an 88-tall preview at
    // radius 6 inside each one.
    expect(await page.locator('[role="radiogroup"]').evaluate((n) => getComputedStyle(n).columnGap)).toBe('14px');
    const preview = await dark.locator('[aria-hidden="true"]').first().evaluate((node) => ({
      height: node.getBoundingClientRect().height,
      radius: getComputedStyle(node).borderRadius,
    }));
    expect(preview.height).toBe(88);
    expect(preview.radius).toBe('6px');
  });
});
